"""Silero VAD 语音活动检测切段

**为什么不用 ffmpeg 的 silencedetect**：它只看能量。而开场音乐往往比人声
还响（实测目标视频：音乐 −13~−35 dB，人声 −26~−30 dB），音乐被当成语音，
切出的段起点早了 7.4 秒，字幕在人还没开口时就冒出来。VAD 用声学特征判断，
能正确跳过音乐、掌声、环境噪声。

⚠️ **权重必须锁 v5.1.2**。master 分支是 v6.x，接口变了会**静默失效** ——
在明显是人声的音频上概率只有 0.056，不报错，只是"检出 0 段"。所以
registry 里锁的是 v5.1.2 的标签与 sha256，`default_model_path()` 只从
数据目录取。

⚠️ **检出 0 段一律当异常**。0 段有两种成因（素材真没人说话 / 用错了模型
版本），用户分不清。静默返回空列表会让后者伪装成前者 —— 用户拿到一份
空字幕，却不知道为什么。

分两层：`segments_from_probs` 是纯函数（概率序列 → 区间），
`segments` 才碰模型与文件。合成音频（正弦波、白噪）基本不触发 Silero，
想靠"造合成语音"来测切段是不现实的；把概率数组当输入，逻辑才测得动。
"""
from __future__ import annotations

import array
import os
import wave
from typing import Optional, Sequence

import numpy as np

from . import registry, runtime

MODEL_ENV = "VIDSUB_VAD_MODEL"

# 阈值与 r2t2-test/srt/vad.py 实测通过的那套一致。这几个数不是拍的：
# 单一阈值会在阈值附近反复开合，把一段话切成一堆碎片，所以用滞回。
THRESH = 0.5          # 高于它才算语音开始
THRESH_END = 0.35     # 低于它才算语音结束
MIN_SPEECH = 0.25     # 短于它的响动丢掉（咳嗽、翻页）
MIN_SIL = 0.35        # 短于它的间隔合并（那是气口，不是停顿）
PAD = 0.10            # 段前后留余量，防止吃掉首尾音素
MAX_SEG = 10.0        # 单段上限：不切的话一条字幕在画面上要占四五行
CUT_SEARCH = 0.8      # 在切点附近 ± 这个范围里找能量最低处
CUT_WIN = 0.05        # 能量搜索的窗口长度
MIN_PIECE = 0.2       # 切出来的碎片短于它就丢弃


class NoSpeechError(RuntimeError):
    """没检测到任何语音段。

    归到 RuntimeError 下面是为了让接口层能统一转成可读消息 —— 这不是
    "程序坏了"，而是"这段素材做不了字幕"。
    """


def default_model_path() -> str:
    """默认权重路径：数据目录下的 vad/。

    `VIDSUB_VAD_MODEL` 可覆盖（测试与嵌入式调用用）。路径**不含空格**
    这条约束由 `runtime.default_model_root()` 保证。
    """
    override = os.environ.get(MODEL_ENV)
    if override:
        return override
    return os.path.join(runtime.default_model_root(),
                        registry.rel_path(registry.get("vad")))


def speech_probs(wav: str, model_path: Optional[str] = None):
    """逐帧语音概率。返回 `(probs, 帧长秒, 采样率, PCM float32)`。

    Silero v5 的输入是 512 采样窗口 @16kHz；其它采样率要用 256。窗口与
    采样率不匹配时模型**不报错、只是概率全低**，所以 sr 必须跟着音频走。
    """
    path = model_path or default_model_path()
    if not os.path.exists(path):
        raise NoSpeechError(
            f"找不到 VAD 权重：{path}\n"
            f"  首次运行需要先在下载页把权重拉下来，"
            f"或用 {MODEL_ENV} 指定已有的权重。")

    with wave.open(wav, "rb") as w:
        sr = w.getframerate()
        pcm = np.frombuffer(array.array("h", w.readframes(w.getnframes())),
                            dtype=np.int16).astype(np.float32) / 32768.0

    win = 512 if sr == 16000 else 256

    # 延迟导入：onnxruntime 只在真正要用 VAD 时才需要加载
    import onnxruntime as ort

    sess = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
    state = np.zeros((2, 1, 128), dtype=np.float32)
    sr_arg = np.array(sr, dtype=np.int64)

    probs = []
    for i in range(0, len(pcm) - win, win):
        out, state = sess.run(None, {
            "input": pcm[i:i + win].reshape(1, -1),
            "state": state,
            "sr": sr_arg,
        })
        probs.append(float(out[0][0]))
    return probs, win / sr, sr, pcm


def _cuts(pcm, sr: int, a: float, b: float, max_seg: float) -> list:
    """在 `(a, b)` 内找出若干切点。

    有 PCM 时在 ±`CUT_SEARCH` 窗口里找 **RMS 最低的 50ms 处**下刀 ——
    按固定时长均分会切断单词（实测在 "Don't you feel..." 中间断开，
    相邻两条字幕重复同一个词）。没有 PCM 时退化为均分。
    """
    if pcm is None:
        n = int((b - a) // max_seg)
        return [a + max_seg * (i + 1) for i in range(n)
                if a + max_seg * (i + 1) < b - 1e-9]

    step = max(int(CUT_WIN * sr), 1)
    lo_all, hi_all = int(a * sr), int(b * sr)
    cuts = []
    t = a + max_seg
    while t < b - 1.2:
        lo = max(int((t - CUT_SEARCH) * sr), lo_all)
        hi = min(int((t + CUT_SEARCH) * sr), hi_all)
        best_i, best_e = None, None
        i = lo
        while i + step < hi:
            e = float(np.sum(pcm[i:i + step] ** 2))
            if best_e is None or e < best_e:
                best_e, best_i = e, i
            i += step
        if best_i is None:
            break
        cuts.append(best_i / sr)
        t = best_i / sr + max_seg
    return cuts


def segments_from_probs(probs: Sequence[float], frame: float, total: float,
                        pcm=None, sr: int = 16000,
                        thresh: float = THRESH, thresh_end: float = THRESH_END,
                        min_speech: float = MIN_SPEECH, min_sil: float = MIN_SIL,
                        pad: float = PAD, max_seg: float = MAX_SEG) -> list:
    """概率序列 → 语音区间 `[(起, 止)]`。纯函数，不碰模型也不碰文件。

    四步：滞回判定 → 合并气口 → 丢过短 + 留余量 → 切开过长。
    顺序不能换：先合并再判长度，否则"两句话中间的气口"会被当成两段
    短语音各自丢掉。
    """
    # 1) 滞回：高于 thresh 开始、低于 thresh_end 才结束
    raw = []
    cur = None
    for i, p in enumerate(probs):
        t = i * frame
        if cur is None and p >= thresh:
            cur = t
        elif cur is not None and p < thresh_end:
            raw.append((cur, t + frame))
            cur = None
    if cur is not None:
        raw.append((cur, len(probs) * frame))

    # 2) 间隔过短的相邻段合并
    merged = []
    for a, b in raw:
        if merged and a - merged[-1][1] < min_sil:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))

    # 3) 丢过短的响动，前后各留一点余量
    segs = []
    for a, b in merged:
        if b - a < min_speech:
            continue
        segs.append((max(0.0, a - pad), min(total, b + pad)))

    # 4) 过长的段切开
    out = []
    for a, b in segs:
        if b - a <= max_seg:
            out.append((a, b))
            continue
        pts = [a] + _cuts(pcm, sr, a, b, max_seg) + [b]
        for i in range(len(pts) - 1):
            if pts[i + 1] - pts[i] > MIN_PIECE:
                out.append((pts[i], pts[i + 1]))
    return out


def segments(wav: str, model_path: Optional[str] = None, **kw) -> list:
    """切出语音区间。

    **检出 0 段时抛 `NoSpeechError`，绝不返回空列表** —— 见模块开头的说明。
    """
    probs, frame, sr, pcm = speech_probs(wav, model_path=model_path)
    total = len(pcm) / sr
    out = segments_from_probs(probs, frame, total, pcm=pcm, sr=sr, **kw)

    if not out:
        raise NoSpeechError(
            f"没检测到任何语音（音频 {total:.1f} 秒，{len(probs)} 帧）。\n"
            f"  两种可能，请分别确认：\n"
            f"    1. 素材里确实没有人说话（纯音乐 / 环境音 / 静音）\n"
            f"    2. VAD 权重版本不对 —— 必须用 v5.1.2；v6.x 接口变了会\n"
            f"       静默失效，在明显是人声的音频上概率只有 0.056\n"
            f"  当前权重：{model_path or default_model_path()}")
    return out
