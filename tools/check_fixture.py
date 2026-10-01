"""检查测试素材是否满足 VAD 的硬前提：语音占比足够高、无背景音乐干扰。

为什么必须查这一项：已实测 ffmpeg silencedetect 只看能量，而**开场音乐
(−13~−35 dB) 比人声(−26~−30 dB)还响**，音乐被当成语音，切出的段起点早了 7.42 秒。
素材如果带配乐，E2E 就会不稳定甚至误报失败。

用法: python check_fixture.py <音频文件> [期望语音占比下限]
"""
import os
import subprocess
import sys
import tempfile
import wave

import numpy as np


def pcm_of(path: str):
    """读成 16k 单声道 float32"""
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-nostdin",
                        "-i", path, "-vn", "-ar", "16000", "-ac", "1",
                        "-c:a", "pcm_s16le", tmp.name],
                       check=True, capture_output=True, timeout=300)
        with wave.open(tmp.name, "rb") as w:
            sr = w.getframerate()
            data = np.frombuffer(w.readframes(w.getnframes()),
                                 dtype=np.int16).astype(np.float32) / 32768.0
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    return data, sr


def model_path() -> str:
    return os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "models", "silero_vad.onnx"))


def model_available() -> bool:
    """是否装有 Silero VAD 模型。

    必须用 v5.1.2 标签的模型：master 分支是 v6.x，接口变了会**静默失效**
    —— 在明显是人声的音频上概率只有 0.056，不报错，只是"检出 0 段"。
    """
    return os.path.exists(model_path())


def speech_ratio(path: str) -> tuple:
    """返回 (语音占比, 语音秒数, 总秒数, 段数)

    优先用 Silero VAD。缺模型时降级为能量估算，但仅作兜底 ——
    能量法正是"背景音乐被当成语音"这个 bug 的成因。
    """
    model = model_path()
    data, sr = pcm_of(path)
    total = len(data) / sr
    if total < 1.0:
        raise RuntimeError(f"音频太短（{total:.2f}s），无法评估语音占比")

    if not os.path.exists(model):
        # 兜底：高于 -35dB 视为语音。这个阈值正好落在背景音乐的能量区间，
        # 所以只能用于粗筛，不能当作通过依据。
        win = int(0.05 * sr)
        n = max(1, len(data) // win)
        frames = data[:n * win].reshape(n, win)
        rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
        db = 20 * np.log10(rms)
        speech = float((db > -35).mean())
        return speech, speech * total, total, 0

    import onnxruntime as ort
    sess = ort.InferenceSession(model, providers=["CPUExecutionProvider"])
    state = np.zeros((2, 1, 128), dtype=np.float32)
    sr_arg = np.array(sr, dtype=np.int64)
    win = 512 if sr == 16000 else 256
    speech_frames = 0
    total_frames = 0
    segs, cur = 0, False
    for i in range(0, len(data) - win, win):
        out, state = sess.run(None, {"input": data[i:i + win].reshape(1, -1),
                                     "state": state, "sr": sr_arg})
        p = float(out[0][0])
        total_frames += 1
        if p >= 0.5:
            speech_frames += 1
        if p >= 0.5 and not cur:
            segs += 1
            cur = True
        elif p < 0.35 and cur:
            cur = False
    ratio = speech_frames / total_frames if total_frames else 0.0
    return float(ratio), float(ratio * total), total, segs


if __name__ == "__main__":
    path = sys.argv[1]
    min_ratio = float(sys.argv[2]) if len(sys.argv) > 2 else 0.30
    r, secs, total, segs = speech_ratio(path)
    print(f"文件      {path}")
    print(f"总时长    {total:.1f} 秒")
    print(f"语音占比  {r*100:.1f}%   语音 {secs:.1f} 秒   段数 {segs}")
    print(f"下限      {min_ratio*100:.1f}%")
    print("结论      " + ("✅ 适合做 E2E 素材" if r >= min_ratio
                          else "❌ 语音占比过低，素材可能含配乐或静音"))
