"""音轨抽取与媒体探测

素材全部用 ffmpeg 现场合成，不依赖网络、不依赖 E2E 素材缓存 —— 这两个接缝
的行为（采样率、声道数、时长、分辨率）用合成输入就能完整验证，而且合成素材
每次一模一样，不会因为缓存里的文件变了而飘。

一个刻意的断言：**纯音频文件的分辨率必须是 None 而不是 0**。
页面要显示"时长 · 分辨率 · 体积"，若纯音频报成 0×0，用户会看到
"0×0"这种没意义的东西。
"""
import json
import os
import subprocess
import wave

import pytest

from vidsub import audio


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180, **kw)


def make_video(path, seconds=3.0, size="320x240", rate=10, freq=440):
    """造一段**带音轨**的视频。

    音轨是必须的：上传的素材没有音轨就没法识别，而 ffmpeg 对不存在的流
    不报错、只会静默产出空音轨，必须让素材本身就有声音才能验到真东西。
    """
    r = _run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
        "-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}:duration={seconds}",
        "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
    ])
    assert r.returncode == 0, f"合成素材失败：{r.stderr[-400:]}"
    return str(path)


def make_wav(path, seconds=3.0, sr=16000, freq=440):
    r = _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
              "-i", f"sine=frequency={freq}:duration={seconds}",
              "-ar", str(sr), "-ac", "1", "-c:a", "pcm_s16le", str(path)])
    assert r.returncode == 0, f"合成音频失败：{r.stderr[-400:]}"
    return str(path)


# --- 媒体探测 -----------------------------------------------------------

def test_probe_media_reports_duration_resolution_and_size(tmp_path):
    """页面要显示的三个数字，来源必须是 ffprobe 而不是我们猜的"""
    video = make_video(tmp_path / "in.mp4", seconds=3.0, size="320x240")
    info = audio.probe_media(video)

    assert info.duration == pytest.approx(3.0, abs=0.25)
    assert (info.width, info.height) == (320, 240)
    assert info.size_bytes == os.path.getsize(video)
    assert info.has_video is True
    assert info.has_audio is True


def test_probe_media_on_audio_only_reports_no_resolution(tmp_path):
    """纯音频没有画面：分辨率必须是 None，不能报 0×0"""
    wav = make_wav(tmp_path / "a.wav", seconds=2.0)
    info = audio.probe_media(wav)

    assert info.has_audio is True
    assert info.has_video is False
    assert info.width is None
    assert info.height is None


def test_probe_media_detects_missing_audio_track(tmp_path):
    """没有音轨的素材要能被认出来。

    识别不到音轨等于识别不出任何字幕，必须在**上传时**就说清楚，
    而不是等跑完 VAD 得到 0 段再报一个含糊的错。
    """
    silent = tmp_path / "mute.mp4"
    r = _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
              "-i", "testsrc=size=160x120:rate=10:duration=2",
              "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
              "-an", str(silent)])
    assert r.returncode == 0, f"合成无音轨素材失败：{r.stderr[-400:]}"

    info = audio.probe_media(str(silent))
    assert info.has_video is True
    assert info.has_audio is False


def test_probe_media_rejects_missing_file(tmp_path):
    """文件不存在要给可读错误，不能抛一个 ffprobe 的原始堆栈"""
    with pytest.raises(audio.MediaError) as e:
        audio.probe_media(str(tmp_path / "nope.mp4"))
    assert "nope.mp4" in str(e.value)


def test_probe_media_rejects_non_media_file(tmp_path):
    """随便一个文本文件不是媒体：要报错，不能返回一个 0 时长的 MediaInfo"""
    junk = tmp_path / "notmedia.mp4"
    junk.write_text("这不是视频", encoding="utf-8")
    with pytest.raises(audio.MediaError):
        audio.probe_media(str(junk))


# --- 抽音轨 -------------------------------------------------------------

def test_extract_audio_produces_16k_mono_pcm(tmp_path):
    """识别模型只吃 16k 单声道 PCM。

    采样率或声道数不对时 llama-server 不会报错，只会给出错乱的转写 ——
    所以这两项必须被断言住。
    """
    video = make_video(tmp_path / "in.mp4", seconds=3.0)
    wav = str(tmp_path / "out.wav")

    assert audio.extract_audio(video, wav) == wav
    assert os.path.exists(wav)

    with wave.open(wav, "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2                      # pcm_s16le
        assert w.getnframes() / 16000 == pytest.approx(3.0, abs=0.25)


def test_extract_audio_overwrites_existing_target(tmp_path):
    """重复跑同一个作业不能因为目标已存在就失败（-y 必须带上）"""
    video = make_video(tmp_path / "in.mp4", seconds=2.0)
    wav = str(tmp_path / "out.wav")
    make_wav(wav, seconds=9.0)                            # 先放一个不同的旧文件

    audio.extract_audio(video, wav)
    with wave.open(wav, "rb") as w:
        assert w.getnframes() / 16000 == pytest.approx(2.0, abs=0.25)


def test_extract_audio_fails_loudly_on_source_without_audio(tmp_path):
    """源没有音轨时必须报错。

    ffmpeg 对"不存在的流"不报错，会静默产出一个空音轨 —— 之后 VAD 检出
    0 段，用户看到的却是一个含糊的"没有检测到语音"。要在源头就说清楚。
    """
    silent = tmp_path / "mute.mp4"
    r = _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
              "-i", "testsrc=size=160x120:rate=10:duration=2",
              "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
              "-an", str(silent)])
    assert r.returncode == 0, r.stderr[-400:]

    with pytest.raises(audio.MediaError) as e:
        audio.extract_audio(str(silent), str(tmp_path / "out.wav"))
    assert "音轨" in str(e.value)


# --- 切片 ---------------------------------------------------------------

def test_slice_wav_keeps_requested_duration(tmp_path):
    """切段的时长要准：时间轴直接由它推导，偏了字幕就对不上"""
    full = make_wav(tmp_path / "full.wav", seconds=10.0)
    seg = str(tmp_path / "seg.wav")

    assert audio.slice_wav(full, seg, start=2.0, dur=1.5) == seg
    with wave.open(seg, "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        assert w.getnframes() / 16000 == pytest.approx(1.5, abs=0.15)


def test_slice_wav_does_not_exceed_source_end(tmp_path):
    """切到文件尾之外时只给到结尾，不能报错也不能补出一段静音"""
    full = make_wav(tmp_path / "full.wav", seconds=3.0)
    seg = str(tmp_path / "tail.wav")

    audio.slice_wav(full, seg, start=2.5, dur=5.0)
    with wave.open(seg, "rb") as w:
        dur = w.getnframes() / w.getframerate()
    assert 0.2 <= dur <= 0.8


# --- ffprobe 输出解析 ---------------------------------------------------

def test_probe_parses_ffprobe_json_without_shelling_out(monkeypatch, tmp_path):
    """解析逻辑要能吃下 ffprobe 的真实输出形状。

    直接用真文件测更可靠，但这里额外钉住一次解析契约：一旦 ffprobe 换输出
    格式（例如把 duration 变成字符串），要在这里失败而不是在用户那里。
    """
    payload = {
        "streams": [
            {"codec_type": "video", "width": 1280, "height": 720},
            {"codec_type": "audio"},
        ],
        "format": {"duration": "12.345", "size": "4096"},
    }
    fake = tmp_path / "x.mp4"
    fake.write_bytes(b"\x00" * 4096)

    monkeypatch.setattr(audio, "_ffprobe_json", lambda p: json.loads(
        json.dumps(payload)))
    info = audio.probe_media(str(fake))

    assert info.duration == pytest.approx(12.345)
    assert (info.width, info.height) == (1280, 720)
    assert info.size_bytes == 4096
