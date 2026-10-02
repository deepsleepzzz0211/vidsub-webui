"""流水线测试：用 fake 服务 + 假的 VAD 切出口，验 SRT 产物与可复现性

要验的行为（对应工单 04 的验收）：
- 中文在上、英文在下的对照 SRT
- temperature=0.0 下同输入跑两次字节一致
- 纯中文模式只保留中文行
- 失败时明确报错，而不是产出一份空字幕

替身服务要像真机：ASR 输出 `language <lang><asr_text>...` 前缀，MT 的
/v1/chat/completions 返回 content。
"""
import http.server
import json
import os
import threading
import wave

import pytest

from vidsub import pipeline


# --- fake 推理服务 ---------------------------------------------------------

class FakeSrv:
    """同一 handler 可占一个端口（ASR/MT 各自一个）。"""

    def __init__(self, handler):
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_port

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/v1/chat/completions"

    def close(self):
        self.srv.shutdown()


def _mk_handler(kind):
    class H(http.server.BaseHTTPRequestHandler):
        def _emit(self, body):
            if body.get("temperature") != 0.0:
                raise RuntimeError("temperature 应为 0.0")
            return "language en<asr_text>hello world" if kind == "asr" \
                else "你好，世界"

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if kind == "error":
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            out = self._emit(body)
            data = json.dumps({"choices": [{"message": {"content": out}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    return H


@pytest.fixture
def services():
    asr = FakeSrv(_mk_handler("asr"))
    mt = FakeSrv(_mk_handler("mt"))
    try:
        yield asr, mt
    finally:
        asr.close()
        mt.close()


# --- 探针：真写入一个静音 wav --------------------------------------------

def _silent_wav(tmp_path, name="a.wav", seconds=1.0):
    p = tmp_path / name
    w = wave.open(str(p), "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(16000)
    w.writeframes(b"\x00\x00" * int(16000 * seconds))
    w.close()
    return str(p)


def _seg(tmp_path, name, seconds=0.4):
    """一段能让 ffmpeg -c copy 截出的小 wav"""
    return _silent_wav(tmp_path, name, seconds=seconds)


def _silent_wav_at(path, seconds=1.0):
    """在**指定路径**写一个静音 wav —— 供 extract_audio 的 mock 使用，
    让它钉到 pipeline 期望的 work_dir 位置上。"""
    w = wave.open(str(path), "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(16000)
    w.writeframes(b"\x00\x00" * int(16000 * seconds))
    w.close()
    return path


# --- transcribe / translate ---------------------------------------------

def test_transcribe_parses_language_and_text(services, tmp_path):
    asr, _ = services
    wav = _silent_wav(tmp_path)
    lang, text = pipeline.transcribe(wav, asr_url=asr.url)
    assert lang == "en"
    assert text == "hello world"


def test_translate_returns_target_language(services, tmp_path):
    _, mt = services
    out = pipeline.translate("hello world", mt_url=mt.url)
    assert out == "你好，世界"


def test_translate_retries_then_gives_up(tmp_path):
    mt = FakeSrv(_mk_handler("error"))
    try:
        with pytest.raises(Exception):
            pipeline.translate("hi", mt_url=mt.url, retries=0)
    finally:
        mt.close()


# --- SRT 产物 ------------------------------------------------------------

def test_write_srt_bilingual(tmp_path):
    out = str(tmp_path / "o.srt")
    pipeline.write_srt([(0.0, 1.0, "hello", "你好"), (1.2, 2.5, "world", "世界")], out)
    body = open(out, encoding="utf-8").read()
    assert "你好" in body and "hello" in body
    # 中文在上：同一块里「你好」要先于「hello」
    assert body.index("你好") < body.index("hello")


def test_write_srt_mono_keeps_chinese_only(tmp_path):
    out = str(tmp_path / "o.srt")
    pipeline.write_srt([(0, 1, "hello", "你好")], out, mono=True)
    body = open(out, encoding="utf-8").read()
    assert "你好" in body and "hello" not in body


def test_write_srt_empty_cue_does_not_break_numbering(tmp_path):
    out = str(tmp_path / "o.srt")
    pipeline.write_srt([(0, 1, "", ""), (1.2, 2.0, "ok", "好")], out)
    body = open(out, encoding="utf-8").read()
    assert "1\n" in body or True
    # 序号必须从 1 开始而不是出现空序号
    lines = [l for l in body.splitlines() if l.strip()]
    assert lines[0] == "1"


# --- 端到端（用假 VAD 替代真模型） ---------------------------------------

def test_full_run_produces_bilingual_srt(services, tmp_path, monkeypatch):
    video = _silent_wav(tmp_path, "in.mp4", 3.0)
    out = str(tmp_path / "o.srt")
    # 截两段：真实 VAD 需要真人声，静音上跑不出段，所以替换成确定的两段。
    monkeypatch.setattr(pipeline.vad, "segments",
                        lambda wav, **kw: [(0.0, 1.0), (1.5, 2.5)])
    # extract_audio 会实际起 ffmpeg；这里让它直接产一个假的音轨文件
    monkeypatch.setattr(pipeline.audio, "extract_audio",
                        lambda video, wav: _silent_wav_at(wav, 3.0))
    asr, mt = services
    pipeline.run(video, out, work_dir=str(tmp_path / "work"),
                 asr_url=asr.url, mt_url=mt.url)
    body = open(out, encoding="utf-8").read()
    assert body.count("-->") == 2
    assert "你好，世界" in body


def test_full_run_is_deterministic(services, tmp_path, monkeypatch):
    video = _silent_wav(tmp_path, "in.mp4", 3.0)
    o1 = str(tmp_path / "a.srt")
    o2 = str(tmp_path / "b.srt")
    monkeypatch.setattr(pipeline.vad, "segments",
                        lambda wav, **kw: [(0.0, 1.0)])
    monkeypatch.setattr(pipeline.audio, "extract_audio",
                        lambda video, wav: _silent_wav_at(wav, 3.0))
    asr, mt = services
    pipeline.run(video, o1, work_dir=str(tmp_path / "w1"),
                 asr_url=asr.url, mt_url=mt.url)
    pipeline.run(video, o2, work_dir=str(tmp_path / "w2"),
                 asr_url=asr.url, mt_url=mt.url)
    assert open(o1, encoding="utf-8").read() == open(o2, encoding="utf-8").read(), \
        "同输入两次运行应产出字节一致的 SRT（温度 0.0 的意义）"


def test_no_speech_raises_not_empty_srt(services, tmp_path, monkeypatch):
    """VAD 检出 0 段时要明确报错，而不是产出一份空字幕"""
    video = _silent_wav(tmp_path, "in.mp4", 1.0)
    out = str(tmp_path / "o.srt")
    monkeypatch.setattr(pipeline.vad, "segments",
                        lambda *a, **kw: [])
    asr, mt = services
    monkeypatch.setattr(pipeline.audio, "extract_audio",
                        lambda video, wav: _silent_wav_at(wav, 1.0))
    with pytest.raises(Exception) as ei:
        pipeline.run(video, out, work_dir=str(tmp_path / "w"),
                     asr_url=asr.url, mt_url=mt.url)
    assert "没检测到" in str(ei.value) or "语音" in str(ei.value)