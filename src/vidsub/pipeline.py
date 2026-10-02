"""上传视频 → 双语字幕 的完整流水线（04 号票的核心链路）

视频 → 抽音轨（16k 单声道）→ Silero VAD 切段 → 逐段 ASR 识别 →
逐段翻译 → 中文在上、英文在下的对照 SRT。

两条硬约束落地：
1. **工作目录不含空格**。llama-server 收到含空格的模型绝对路径会报
   invalid argument；ffmpeg 的 subtitles= 滤镜也一样。VAD 模型路径来自
   runtime.default_model_root()（已躲开空格），所有中间文件都写进
   job 目录下，job 目录本身由 jobs.py 保证不在空格路径里。
2. **采样温度 0.0**。同一输入跑两次，产出必须字节级一致的字幕，
   否则重跑出来的字幕跟前一版对不上，是噩梦。

外部请求一律不走系统代理（_OPENER 已清空），否则本地 llama-server 的
健康检查会被 http_proxy 劫持。
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from . import audio, styles, vad

ASR_PORT = 8081
MT_PORT = 8082
ASR_URL = f"http://127.0.0.1:{ASR_PORT}/v1/chat/completions"
MT_URL = f"http://127.0.0.1:{MT_PORT}/v1/chat/completions"

# 本地服务不走系统代理 —— 否则 http_proxy 会把 localhost 请求劫到代理上
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

LANG_RE = re.compile(r"language\s+(\w+)\s*<asr_text>(.*)", re.S)


class PipelineError(RuntimeError):
    """流水线失败，消息直接给用户。"""


def _post(payload: dict, url: str, timeout: float = 600) -> str:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    raw = _OPENER.open(req, timeout=timeout).read()
    body = json.loads(raw)
    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise PipelineError(f"推理服务返回了不认识的结构：{raw[:300]!r}")


def transcribe(wav: str, asr_url: str = ASR_URL,
               temp: float = 0.0) -> tuple[str, str]:
    """识别一段 wav。返回 (language, text)。

    R2T2 的输出形如 `language en<asr_text>hello world`。
    """
    with open(wav, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "Transcribe the audio."},
        {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}},
    ]}], "temperature": temp, "max_tokens": 512}
    out = _post(payload, asr_url)
    m = LANG_RE.search(out)
    return (m.group(1), m.group(2).strip()) if m else ("", out.strip())


def translate(text: str, target: str = "Chinese",
              history: Optional[list] = None,
              mt_url: str = MT_URL, temp: float = 0.0,
              retries: int = 2) -> str:
    """翻译一段文本。

    history=[(src, dst), ...] —— 前几段的原文/译文，作为背景交给模型，
    改善指代和人名的一致性（A/B 实测在本地 ctx_n=0 时开上下文反而退化为
    重复上句译文，所以**默认关闭**：传了 history 才带背景）。
    """
    if not text.strip():
        return ""
    if history:
        bg = "\n".join(f"{i + 1}. {s}\n   -> {d}" for i, (s, d) in enumerate(history))
        prompt = (
            f"[Background Information]\n{bg}\n\n"
            f"Please translate the following text into {target}, taking the provided "
            f"background information into consideration. Note that you should only output "
            f"the translated result without any additional explanation.\n\n"
            f"[Source Text]\n{text}")
    else:
        prompt = (
            f"Translate the following text into {target}. Note that you should only "
            f"output the translated result without any additional explanation:\n\n{text}")
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temp, "top_p": 0.6, "top_k": 20, "max_tokens": 1024,
    }
    last = ""
    for attempt in range(retries + 1):
        try:
            return _post(payload, mt_url).strip()
        except PipelineError:
            last = "服务返回异常"
            time.sleep(1)
        except (urllib.error.URLError, TimeoutError):
            last = "推理服务超时"
            time.sleep(1)
    raise PipelineError(last)


def fmt_ts(sec: float) -> str:
    return styles._ts(sec)


def write_srt(cues: list, path: str, mono: bool = False,
              style: Optional[str] = None) -> None:
    """渲染 SRT。中文在上、英文在下（mono 只留中文行）。

    长 cue 会先做分段（07）—— 否则一整条字会盖掉整个幻灯片。
    序号与时间轴必须保留 —— 只拼文本行产出的不是合法 SRT。
    """
    from . import styles
    style = style or ("mono" if mono else "bilingual")
    body = styles.format_srt(cues, style=style)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)


def _eta(t0: float, done: int, total: Optional[int] = None) -> float:
    """按已完成段的**平均**耗时估剩余秒数，取不到就返回 -1。

    用平均而不是瞬时速度：单段耗时抖动很大（一段长句可能 20 秒，下一段
    0.3 秒），瞬时速度算出的 ETA 会满屏跳。平均至少是单调收敛的。

    前几段样本不足（done < 2）时不给数 —— 那时候的估计纯属噪声，
    显示一个"还剩 3 秒"然后变成"还剩 40 分钟"比不给更糟。
    """
    if done < 2:
        return -1.0
    per = (time.monotonic() - t0) / done
    if total is None:
        return per
    return max(0.0, per * (total - done))


def run(video_path: str, out_srt: str,
        work_dir: Optional[str] = None,
        progress: Optional[Callable[..., None]] = None,
        on_stage: Optional[Callable[[str], None]] = None,
        asr_url: str = ASR_URL, mt_url: str = MT_URL,
        vad_model: Optional[str] = None,
        mono: bool = False, target: str = "Chinese") -> str:
    """完整跑一条：抽音轨 → VAD 切段 → 逐段识别 → 逐段翻译 → SRT。

    返回 out_srt 路径。工作目录默认取视频同目录下的 work，由调用方保证
    不含空格（jobs.py 负责）。失败时抛出 `MediaError` / `NoSpeechError` /
    `PipelineError`，消息面向用户。

    `on_stage` 报大阶段（抽音轨/切分/翻译/写出），`progress(i, total, stage,
    translated)` 报段落级进度 —— 05 号票要求页面上能看到"现在在干嘛"，
    光有 `识别中 3/226` 是不够的：抽音轨和切分在 226 段的大视频上要花掉
    好几分钟，那段时间里进度条一动不动会让人以为卡死了。
    """
    def stage(label: str) -> None:
        if on_stage:
            on_stage(label)

    if work_dir is None:
        work_dir = os.path.join(os.path.dirname(os.path.abspath(video_path)), "work")
    os.makedirs(work_dir, exist_ok=True)

    stage("抽取音轨")
    wav = os.path.join(work_dir, "audio.wav")
    audio.extract_audio(video_path, wav)

    stage("切分语音")
    segs = vad.segments(wav, model_path=vad_model)
    if not segs:
        # 正常路径里 vad.segments 检出 0 段会自己抛 NoSpeechError；
        # 这里兜一层：万一上游被替换/绕过，不要静默产出一份空字幕。
        from .vad import NoSpeechError
        raise NoSpeechError("流水线拿到 0 个语音段，无法产出字幕")

    stage(f"识别并翻译（共 {len(segs)} 段）")
    cues = []
    t0 = time.monotonic()
    for i, (a, b) in enumerate(segs):
        if progress:
            progress(i, len(segs), "识别并翻译", i, _eta(t0, i, len(segs)))
        part = os.path.join(work_dir, f"seg_{i:04d}.wav")
        audio.slice_wav(wav, part, a, b - a)
        _, src = transcribe(part, asr_url=asr_url)
        dst = translate(src, target=target, mt_url=mt_url)
        cues.append((a, b, src, dst))
        if progress:
            progress(i + 1, len(segs), "识别并翻译", i + 1, _eta(t0, i + 1, len(segs)))

    stage("写出字幕")
    write_srt(cues, out_srt, mono=mono)
    return out_srt