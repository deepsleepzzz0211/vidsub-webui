"""VAD 切段

分两层测：

- **纯函数层** `segments_from_probs`：给一串语音概率就能验完滞回、合并、
  丢过短、过长切分。不碰模型、不碰音频，跑得快且完全确定。
- **模型层** `segments`：只验真实模型上的行为，重点是**纯静音必须抛异常**
  这条硬要求。

为什么要拆这两层：Silero VAD 对合成音频（正弦波、白噪）基本不响应，
想造"合成语音"来测切段是不现实的。把概率数组当输入，逻辑才测得动。
"""
import os
import subprocess
import sys

import numpy as np
import pytest

from vidsub import vad

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "tools"))


def _probs(segments, frame=0.1, total=None):
    """把 [(起, 止)] 的语音区间展开成概率序列，区间外是静音"""
    n = total if total is not None else int(max(b for _, b in segments) / frame) + 5
    p = [0.0] * n
    for a, b in segments:
        for i in range(int(a / frame), min(int(b / frame), n)):
            p[i] = 0.9
    return p


def _loud_pcm(seconds, sr=16000, amp=0.5, quiet=None):
    """一段恒定响度的 PCM，可选在指定区间压成极低能量"""
    pcm = np.full(int(seconds * sr), amp, dtype=np.float32)
    if quiet:
        a, b = quiet
        pcm[int(a * sr):int(b * sr)] = 0.001
    return pcm


# --- 纯函数层 -----------------------------------------------------------

def test_merges_bursts_separated_by_a_short_gap():
    """间隔小于 min_sil 的两段要合成一段。

    不断开的两句之间那个 0.1~0.3 秒的气口不是真停顿，按它切会把一句话
    劈成两条字幕。
    """
    probs = _probs([(0.5, 1.5), (1.7, 2.7)])
    segs = vad.segments_from_probs(probs, frame=0.1, total=3.0)

    assert len(segs) == 1
    a, b = segs[0]
    assert a == pytest.approx(0.4, abs=0.02)      # 前留 pad
    assert b == pytest.approx(2.9, abs=0.02)      # 后留 pad


def test_keeps_bursts_separated_by_a_real_pause():
    """间隔超过 min_sil 的是真停顿，必须保持两段（上一条的对照）"""
    probs = _probs([(0.5, 1.5), (2.5, 3.5)])
    segs = vad.segments_from_probs(probs, frame=0.1, total=4.0)

    assert len(segs) == 2


def test_drops_speech_shorter_than_minimum():
    """0.2 秒的响动多半是咳嗽/翻页，不是语音，要丢掉。

    帧长 0.1s，所以单个高概率帧展开出来正好是 0.2s —— 卡在 min_speech
    0.25 的下方。这正是要挡住的那一类。
    """
    probs = _probs([(0.5, 0.65)])
    segs = vad.segments_from_probs(probs, frame=0.1, total=3.0)

    assert segs == []


def test_splits_overlong_speech_into_bounded_pieces():
    """20 秒的连续语音要切成不超过 max_seg 的多段。

    不切的话一条字幕要在画面上占四五行，把幻灯片正文全盖住。
    """
    probs = _probs([(0.0, 20.0)])
    segs = vad.segments_from_probs(probs, frame=0.1, total=20.0,
                                   pcm=None, max_seg=10.0)

    assert len(segs) == 2
    assert all(b - a <= 10.0 + 1e-6 for a, b in segs)
    # 首尾要盖住整段，中间不能有空隙
    assert segs[0][0] == pytest.approx(0.0, abs=1e-6)
    assert segs[-1][1] == pytest.approx(20.0, abs=1e-6)
    assert segs[0][1] == pytest.approx(segs[1][0], abs=1e-6)


def test_cuts_overlong_speech_at_the_quietest_point():
    """有 PCM 时切点要落在能量最低处，而不是按固定时长均分。

    均分会切断单词（实测在 "Don't you feel..." 中间断开，相邻两条字幕
    重复同一个词）。这里把安静点故意放在 10.25s —— 均分点是 10.0s，
    两者相差 0.25s，足以区分"真的找了能量最低点"和"就是均分"。
    """
    probs = _probs([(0.0, 20.0)])
    pcm = _loud_pcm(20.0, quiet=(10.25, 10.30))

    segs = vad.segments_from_probs(probs, frame=0.1, total=20.0,
                                   pcm=pcm, sr=16000, max_seg=10.0)

    assert len(segs) == 2
    assert segs[0][1] == pytest.approx(10.25, abs=0.06), \
        f"切点落在 {segs[0][1]:.3f}s，没有贴住 10.25s 的安静点"


def test_even_split_when_no_pcm_available():
    """没有 PCM 时退化为均分 —— 与上一条对照，确认安静点确实起了作用"""
    probs = _probs([(0.0, 20.0)])
    segs = vad.segments_from_probs(probs, frame=0.1, total=20.0,
                                   pcm=None, max_seg=10.0)

    assert segs[0][1] == pytest.approx(10.0, abs=1e-6)


def test_padding_never_goes_out_of_bounds():
    """首段从 0 开始、末段到结尾时，pad 不能把时间轴推成负数或越过总长"""
    probs = _probs([(0.0, 3.0)])
    segs = vad.segments_from_probs(probs, frame=0.1, total=3.0)

    assert segs[0][0] == 0.0
    assert segs[0][1] <= 3.0


def test_no_speech_yields_empty_list_from_pure_layer():
    """纯函数层不负责报错，只如实返回空列表 —— 报错是 segments 的职责"""
    assert vad.segments_from_probs([0.0] * 50, frame=0.1, total=5.0) == []


# --- 模型层 -------------------------------------------------------------

def _make_wav(path, seconds, kind):
    """kind: 'silence' | 'tone'"""
    src = ("anullsrc=r=16000:cl=mono" if kind == "silence"
           else "sine=frequency=440")
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", src,
         "-t", str(seconds), "-ar", "16000", "-ac", "1",
         "-c:a", "pcm_s16le", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120)
    assert r.returncode == 0, r.stderr[-400:]
    return str(path)


def vad_model_or_skip():
    """Silero VAD v5.1.2 权重。

    优先用仓库内 `models/` 下那份（`tools/check_fixture.py` 也用它，本地开发
    时就有），再退回数据目录里的正式落盘位置。两处都没有才跳过。
    """
    for p in (os.path.join(_ROOT, "models", "silero_vad.onnx"),
              vad.default_model_path()):
        if os.path.exists(p):
            return p
    pytest.skip("VAD 权重不存在（先跑一次下载页把权重拉下来，"
                "或放一份到 models/silero_vad.onnx）")


def _fixture_or_skip():
    """E2E 素材（真实人声，2.5 分钟）。

    首次获取需要网络，所以**只在已缓存时才跑**：单元测试不该依赖网络。
    注意这与"素材损坏"不同 —— 损坏属于环境故障，那要让测试失败而不是跳过。
    """
    try:
        import fixture as fx
    except ImportError:                                  # pragma: no cover
        pytest.skip("找不到 tools/fixture.py")
    if not os.path.exists(fx.fixture_path()):
        pytest.skip(f"E2E 素材未缓存（{fx.fixture_path()}），跳过真实语音用例")
    return fx.fixture_path()


def test_silence_raises_instead_of_returning_zero_segments(tmp_path):
    """**检出 0 段必须当异常处理**（本票的硬要求）。

    0 段有两种成因，用户分不清：素材确实没人说话，还是 VAD 用错了版本
    （v6.x 接口变了会静默失效，在明显是人声的音频上概率只有 0.056）。
    静默返回空列表会让第二种情况伪装成第一种，用户拿到一份空字幕却
    不知道为什么。
    """
    wav = _make_wav(tmp_path / "silence.wav", 3.0, "silence")

    with pytest.raises(vad.NoSpeechError) as e:
        vad.segments(wav, model_path=vad_model_or_skip())
    assert "语音" in str(e.value)


def test_pure_tone_is_not_speech(tmp_path):
    """正弦波不是语音，同样要走异常分支。

    这条挡住的是"用错模型版本导致概率全低"—— 上一条的静音本来概率就低，
    这条给的是**有能量的音频**，如果模型坏掉、把什么都说成静音，
    只有这条会失败。
    """
    wav = _make_wav(tmp_path / "tone.wav", 3.0, "tone")

    with pytest.raises(vad.NoSpeechError):
        vad.segments(wav, model_path=vad_model_or_skip())


def test_real_speech_produces_monotonic_bounded_segments(tmp_path):
    """真实人声素材上：段数 > 0、时间轴单调不重叠、每段不超 max_seg"""
    fixture = _fixture_or_skip()

    from vidsub import audio
    wav = str(tmp_path / "probe.wav")
    audio.extract_audio(fixture, wav)
    segs = vad.segments(wav, model_path=vad_model_or_skip())

    assert len(segs) > 0, "真实人声素材上检出 0 段，VAD 可能用错了模型版本"

    prev_end = -1.0
    for a, b in segs:
        assert b > a, f"段 {a}->{b} 时长非正"
        assert a >= prev_end, f"时间轴倒退或重叠：{a} < {prev_end}"
        assert b - a <= 10.0 + 0.25, f"段长 {b - a:.2f}s 超过 max_seg"
        prev_end = b


def test_segments_is_deterministic(tmp_path):
    """同一输入两次必须给出完全一样的切段。

    温度 0.0 换来的可复现性在识别那一步，但切段要是每次都不同，
    整条链路的字节级一致就无从谈起。
    """
    fixture = _fixture_or_skip()

    from vidsub import audio
    wav = str(tmp_path / "probe.wav")
    audio.extract_audio(fixture, wav)
    model = vad_model_or_skip()

    assert vad.segments(wav, model_path=model) == vad.segments(wav, model_path=model)


def test_speech_probs_shape_matches_audio(tmp_path):
    """概率序列长度要和时间轴对得上，帧长必须是 512/16000

    R2T2 只吃 16k；若采样率不是 16k，Silero 的窗口要换成 256，帧长跟着变，
    时间轴就会整体缩放 —— 这类错会让字幕时间对不上而不报任何错。
    """
    wav = _make_wav(tmp_path / "tone.wav", 2.0, "tone")
    probs, frame, sr, pcm = vad.speech_probs(wav, model_path=vad_model_or_skip())

    assert sr == 16000
    assert frame == pytest.approx(512 / 16000)
    assert len(pcm) == pytest.approx(2.0 * 16000, abs=200)
    assert len(probs) == pytest.approx(len(pcm) / 512, rel=0.02)


def test_default_model_path_sits_under_data_dir(tmp_path, monkeypatch):
    """默认权重路径要落在数据目录里（跟着 VIDSUB_DATA_DIR 走）"""
    monkeypatch.setenv("VIDSUB_DATA_DIR", str(tmp_path))
    p = vad.default_model_path()
    assert os.path.normcase(str(tmp_path)) in os.path.normcase(p)
    assert p.endswith(".onnx")
