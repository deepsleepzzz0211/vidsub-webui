"""字幕压制成片（06 号票）

把双语 SRT 烧进视频，得到可直接播放的成片。**核心是把两个静默失败挡在
那一环之前**：

- `-c:a copy` 遇到不存在的音频流**不报错**，会静默产出一个哑片。
  所以压制前和压制后都用 ffprobe 断言音轨存在（前后都要查）。
- ffmpeg 的 subtitles= 滤镜在 Windows 上遇到含空格或盘符冒号的路径会
  解析失败，所以把视频和 SRT 都放进一个**不含空格**的暂存目录，用纯
  文件名引用。这个目录就用工作目录（它由 default_model_root 保证）。

输出用中文优先、中英对照、纯中文三种样式。
"""
from __future__ import annotations

import os
import re
import shutil
from typing import Optional

from . import audio

# 字号是逐档试出来的（每次重压 70 分钟太慢，先在 45 秒片段上抽帧对比）：
#   30px 纯中文 → 占 5 行，正文全被盖住，不可用
#   22px 纯中文 → 只剩 2 行空，字又大又盖在东西上，更糟
#   19px 中英对照 → 4 行，正文还能看清，取这个
SIZE_BILINGUAL = 19
SIZE_MONO = 22

STYLE_TMPL = (
    "FontName=Microsoft YaHei,FontSize={size},"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,"
    "Outline=1.5,Shadow=0,Alignment=2,MarginV=24,MarginL=80,MarginR=80,"
    "LineSpacing=6"
)


class BurnError(RuntimeError):
    """压制失败。消息直接给用户。"""


def _extract_cues(srt_path: str) -> list:
    text = open(srt_path, encoding="utf-8").read()
    blocks = re.split(r"\n\s*\n", text.strip())
    cues = []
    for b in blocks:
        lines = [l for l in b.strip().splitlines() if l.strip()]
        if len(lines) < 3:
            continue
        timing = lines[1]
        zh_lines = [l for l in lines[2:] if re.search(r"[一-鿿]", l)]
        en_lines = [l for l in lines[2:] if not re.search(r"[一-鿿]", l)]
        cues.append({"timing": timing, "zh": zh_lines, "en": en_lines})
    return cues


def srt_for_style(srt_path: str, mono: bool) -> str:
    """生成压制用的 SRT。mono=True 只留中文；否则中英对照。

    必须保留序号与时间轴 —— 只拼文本行产出的不是合法 SRT，ffmpeg 会
    报 "Unable to open"。
    """
    cues = _extract_cues(srt_path)
    out = []
    n = 0
    for c in cues:
        lines = c["zh"] if mono else (c["zh"] + c["en"])
        if not lines:
            continue
        n += 1
        out.append(f"{n}\n{c['timing']}\n" + "\n".join(lines))
    return "\n\n".join(out) + "\n"


def _probe_duration(path: str, cwd: Optional[str] = None) -> float:
    # path 要么按 cwd 解析（源文件传裸名字 + cwd=stage），要么传绝对路径
    # （成品直接传绝对路径，cwd 无所谓）。别统一 basename —— 对绝对路径
    # basename 会在错的目录下找文件。
    r = audio._run([audio.ffprobe(), "-v", "error", "-show_entries", "format=duration",
                    "-of", "csv=p=0", path],
                   cwd=cwd, timeout=120)
    try:
        return float(r.stdout.strip())
    except (ValueError, IndexError) as e:
        raise BurnError(f"ffprobe 读不出时长：{r.stderr.strip()[-200:]}") from e


def burn(video: str, srt_path: str, out: str, mono: bool = False,
         stage_dir: Optional[str] = None) -> str:
    """压制字幕进视频，断言成品同时含画面和声音。

    返回 out 路径。第一个阶段：压前先确认源有音轨；第二个阶段：压完确认
    成品有音轨且时长与源一致 —— 否则静默产出哑片。
    """
    stage = stage_dir or os.path.join(os.environ.get("TEMP", "."), "vidsub_burn")
    os.makedirs(stage, exist_ok=True)

    src_name = "in" + os.path.splitext(video)[1]
    srt_name = "sub.srt" if not mono else "sub_zh.srt"
    shutil.copy2(video, os.path.join(stage, src_name))

    # 生成压制用的 SRT（mono / 双语）
    body = srt_for_style(srt_path, mono)
    with open(os.path.join(stage, srt_name), "w", encoding="utf-8") as f:
        f.write(body)

    # 断言 1：源必须有音轨。没有的话 `-c:a copy` 会静默产出哑片
    if not audio.has_audio(src_name, cwd=stage):
        raise BurnError(
            "源文件没有音频轨，压制出来的成片会是哑的。\n"
            "  常见原因：下载只取了画面没取音轨。")

    font_size = SIZE_MONO if mono else SIZE_BILINGUAL
    style = STYLE_TMPL.format(size=font_size)
    r = audio._run(
        [audio.ffmpeg(), "-y", "-loglevel", "error", "-nostdin",
         "-i", src_name, "-vf", f"subtitles={srt_name}:force_style='{style}'",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
         "-c:a", "copy", os.path.abspath(out)],
        cwd=stage, timeout=3600)
    if r.returncode != 0 or not os.path.exists(out):
        raise BurnError(f"压制失败：\n{r.stderr.strip()[-600:]}")

    # 断言 2：成品必须是有画面+有声音的。`-c:a copy` 不报错，必须主动验证
    if not audio.has_audio(os.path.abspath(out)):
        os.remove(out)
        raise BurnError(
            "成片没有音频轨（压制成功但结果是哑的），已中止。\n"
            "  `-c:a copy` 遇到不存在的音轨流时不会报错，是静默失败。")

    # 断言 3：时长与源一致
    try:
        d_src = _probe_duration(src_name, cwd=stage)
        d_out = _probe_duration(os.path.abspath(out))
        if abs(d_src - d_out) > 1.0:
            raise BurnError(
                f"成片时长与源不符：源 {d_src:.2f}s，成片 {d_out:.2f}s")
    except BurnError:
        raise

    return out