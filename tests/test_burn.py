"""压制成片（06 号票）

要验的行为：
- 成片可播放且**有声音**（核心：`-c:a copy` 静默产哑片要被断言挡住）
- 压前断言源有音轨，压后断言成片有音轨
- ffprobe 证实成品同时含视频流与音频流
- 成片时长与源一致
- 纯中文模式只留中文行
"""
import os
import subprocess

import pytest

from vidsub import audio, burn


def _ffmpeg_run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)


def _make_video(tmp_path, name="v.mp4", seconds=2.0) -> str:
    """生成一段带画面和声音的源视频（testsrc + sine）。"""
    p = tmp_path / name
    r = _ffmpeg_run([
        audio.ffmpeg(), "-y", "-loglevel", "error", "-nostdin",
        "-f", "lavfi", "-i", "testsrc=duration=%s:size=320x240:rate=15" % seconds,
        "-f", "lavfi", "-i", "sine=frequency=220:duration=%s" % seconds,
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(p)])
    assert os.path.exists(p), r.stderr
    return str(p)


def _srt(path, text=None):
    if text is None:
        text = [(0, 1, "你好", "hello")]
    p = os.path.join(path, "s.srt")
    with open(p, "w", encoding="utf-8") as f:
        i = 0
        for a, b, zh, en in text:
            i += 1
            f.write(f"{i}\n00:00:0{int(a)},000 --> 00:00:0{int(b)},000\n{zh}\n{en}\n\n")
    return p


def _streams(path):
    r = _ffmpeg_run([audio.ffprobe(), "-v", "error", "-select_streams", "v",
                     "-show_entries", "stream=codec_name", "-of", "csv=p=0", path])
    v = r.stdout.strip()
    r = _ffmpeg_run([audio.ffprobe(), "-v", "error", "-select_streams", "a",
                     "-show_entries", "stream=codec_name", "-of", "csv=p=0", path])
    a = r.stdout.strip()
    return v, a


def test_burn_produces_video_with_audio(tmp_path):
    v = _make_video(tmp_path)
    srt = _srt(str(tmp_path), text=[(0, 1, "你好", "hello"), (1, 2, "世界", "world")])
    out = str(tmp_path / "out.sub.mp4")
    burn.burn(v, srt, out, stage_dir=str(tmp_path / "stage"))
    vs, asrc = _streams(out)
    assert vs, "成片没有视频流"
    assert asrc, "成片没有音频流 —— 静默产出哑片"


def test_burn_duration_matches_source(tmp_path):
    v = _make_video(tmp_path, seconds=2.0)
    srt = _srt(str(tmp_path), text=[(0, 1, "a", "b")])
    out = str(tmp_path / "out.sub.mp4")
    burn.burn(v, srt, out, stage_dir=str(tmp_path / "stage"))

    def dur(p, cwd=None):
        r = _ffmpeg_run([audio.ffprobe(), "-v", "error", "-show_entries",
                         "format=duration", "-of", "csv=p=0", p])
        return float(r.stdout.strip())

    assert abs(dur(v) - dur(out)) < 1.0


def test_burn_mono_keeps_chinese_only_in_style(tmp_path):
    """纯中文样式生成的 SRT 只应保留中文行。"""
    srt = _srt(str(tmp_path), text=[(0, 1, "你好", "hello")])
    a = burn.srt_for_style(srt, mono=True)
    b = burn.srt_for_style(srt, mono=False)
    assert "你好" in a and "hello" not in a
    assert "你好" in b and "hello" in b


def test_burn_rejects_video_without_audio(tmp_path):
    # 造一个纯视频
    p = tmp_path / "noaudio.mp4"
    _ffmpeg_run([audio.ffmpeg(), "-y", "-loglevel", "error", "-nostdin",
                 "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=15",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(p)])
    srt = _srt(str(tmp_path))
    with pytest.raises(burn.BurnError) as ei:
        burn.burn(str(p), srt, str(tmp_path / "out.mp4"),
                  stage_dir=str(tmp_path / "stage"))
    assert "音轨" in str(ei.value) or "音频" in str(ei.value)