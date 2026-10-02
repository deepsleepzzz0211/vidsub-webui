"""音轨抽取与媒体探测：ffmpeg / ffprobe 的薄封装

两条贯穿全项目的约束在这里落地：

1. **外部进程只接受不含空格的路径**。llama-server 收到含空格的模型绝对路径
   会报 `invalid argument` 直接拒绝启动；ffmpeg 的 `subtitles=` 滤镜在
   Windows 上处理含空格与盘符冒号的路径会失败。本模块自己只把路径当
   `-i` / 输出参数，含空格尚可；但**工作目录由调用方保证不含空格**。
2. **ffprobe 要连 cwd 一起给**。用裸文件名探流时它按调用进程的 CWD 解析，
   不传 cwd 会探不到暂存目录里的文件（曾因此误报"源文件没有音频轨"）。

错误一律包成 `MediaError` 并带可读消息：这里抛出去的字符串会直接显示给
用户，不能是 ffprobe 的原始 stderr。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

# 识别模型只吃 16k 单声道。采样率或声道数不对时 llama-server 不报错，
# 只会给出错乱的转写 —— 所以这两个值写死在这里并有测试守着。
TARGET_SR = 16000

FFMPEG_ENV = "VIDSUB_FFMPEG"
FFPROBE_ENV = "VIDSUB_FFPROBE"


class MediaError(RuntimeError):
    """媒体文件不可用：不存在、不是媒体、缺音轨。

    消息面向用户，接口层直接透传即可。
    """


def _tool(name: str, env_var: str) -> str:
    """定位可执行文件。找不到时给出可读提示而不是 FileNotFoundError。"""
    override = os.environ.get(env_var)
    if override:
        if os.path.isfile(override):
            return override
        raise MediaError(f"{env_var} 指向的文件不存在：{override}")
    found = shutil.which(name)
    if not found:
        raise MediaError(
            f"找不到 {name}。ffmpeg 是抽音轨与压制字幕的必需依赖。\n"
            f"  装好并确保它在 PATH 里，或用 {env_var} 指定完整路径。")
    return found


def ffmpeg() -> str:
    return _tool("ffmpeg", FFMPEG_ENV)


def ffprobe() -> str:
    return _tool("ffprobe", FFPROBE_ENV)


def _run(cmd, cwd: Optional[str] = None, timeout: float = 600):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          cwd=cwd, timeout=timeout)


@dataclass(frozen=True)
class MediaInfo:
    """上传页要展示的信息，也是"这个文件能不能用"的判据。"""
    path: str
    duration: float
    size_bytes: int
    width: Optional[int] = None       # 纯音频是 None，不是 0
    height: Optional[int] = None
    has_video: bool = False
    has_audio: bool = False


def _ffprobe_json(path: str) -> dict:
    """跑 ffprobe 拿原始 JSON。单独抽出来是为了能钉住解析契约。"""
    r = _run([ffprobe(), "-v", "error",
              "-show_entries", "format=duration,size:"
                               "stream=codec_type,width,height",
              "-of", "json", path], timeout=120)
    if r.returncode != 0 or not (r.stdout or "").strip():
        raise MediaError(
            f"无法解析这个文件（ffprobe 读不出音视频流）：{path}\n"
            f"  常见原因：文件损坏、扩展名与实际格式不符、或根本不是媒体文件。\n"
            f"  {r.stderr.strip()[-300:]}")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        raise MediaError(f"ffprobe 的输出不是合法 JSON：{path}") from None


def probe_media(path: str) -> MediaInfo:
    """读时长、分辨率、体积、有没有音轨。

    纯音频的分辨率必须是 None —— 页面要显示"时长 · 分辨率 · 体积"，
    报成 0×0 用户会看到没意义的东西。
    """
    if not os.path.exists(path):
        raise MediaError(f"文件不存在：{path}")
    if os.path.isdir(path):
        raise MediaError(f"这是一个目录，不是媒体文件：{path}")

    data = _ffprobe_json(path)
    streams = data.get("streams") or []
    fmt = data.get("format") or {}

    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if not has_video and not has_audio:
        raise MediaError(
            f"这不是可识别的音视频文件（没找到任何流）：{path}")

    width = height = None
    for s in streams:
        if s.get("codec_type") == "video":
            width = s.get("width") or None
            height = s.get("height") or None
            break

    try:
        duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0

    try:
        size_bytes = int(fmt.get("size"))
    except (TypeError, ValueError):
        size_bytes = os.path.getsize(path)

    return MediaInfo(path=path, duration=duration, size_bytes=size_bytes,
                     width=width, height=height,
                     has_video=has_video, has_audio=has_audio)


def extract_audio(video: str, wav: str) -> str:
    """抽出识别用的 16k 单声道 PCM。

    ⚠️ 源没有音轨时 ffmpeg **不报错**，会静默产出一个空的 wav；之后 VAD
    检出 0 段，用户看到的是一个含糊的"没有检测到语音"。所以这里先探一次
    音轨，缺了就在源头说清楚。
    """
    info = probe_media(video)
    if not info.has_audio:
        raise MediaError(
            f"这个文件没有音轨，无法识别语音：{os.path.basename(video)}\n"
            f"  纯画面或静音文件识别不出任何字幕，请换一个带声音的视频。")

    parent = os.path.dirname(os.path.abspath(wav))
    os.makedirs(parent, exist_ok=True)

    r = _run([ffmpeg(), "-y", "-loglevel", "error", "-nostdin",
              "-i", video, "-vn",
              "-ar", str(TARGET_SR), "-ac", "1", "-c:a", "pcm_s16le", wav])

    # 44 字节是 wav 头。只剩头说明音轨是空的 —— 又是一个静默失败，
    # 必须在这里挡住，不能让它流到 VAD 那里变成"没有检测到语音"。
    if r.returncode != 0 or not os.path.exists(wav) or os.path.getsize(wav) <= 44:
        raise MediaError(
            f"抽取音轨失败：{os.path.basename(video)}\n{r.stderr.strip()[-500:]}")
    return wav


def slice_wav(src: str, dst: str, start: float, dur: float) -> str:
    """切出 `[start, start+dur)` 的一段。

    用**输入侧** `-ss` + `-c copy`：逐段独立解码整条音轨的话，700 段就是
    700 次全量解码。copy 会在包边界上对齐，误差在毫秒级 —— 对段级精度的
    字幕完全够用（本方案本来就不做词级对齐）。
    """
    if dur <= 0:
        raise MediaError(f"切片时长必须为正，收到 {dur}")

    r = _run([ffmpeg(), "-y", "-loglevel", "error", "-nostdin",
              "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src,
              "-c", "copy", dst])
    if r.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) <= 44:
        raise MediaError(
            f"切分音轨失败（{start:.2f}s 起 {dur:.2f}s）：{os.path.basename(src)}\n"
            f"{r.stderr.strip()[-400:]}")
    return dst


def has_audio(path: str, cwd: Optional[str] = None) -> bool:
    """文件是否真的有音频流。

    `cwd` 必须一并传给子进程：用裸文件名探流时 ffprobe 按调用进程的 CWD
    解析，而这里的路径往往是相对于暂存目录的，不传 cwd 会探不到 ——
    曾因此误报"源文件没有音频轨"。

    压制前后的断言都用它（见 06 号票）：`-c:a copy` 遇到不存在的流时
    ffmpeg 不报错，会静默产出一个没有声音的成片。
    """
    r = _run([ffprobe(), "-v", "error", "-select_streams", "a",
              "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
             cwd=cwd, timeout=120)
    return bool((r.stdout or "").strip())
