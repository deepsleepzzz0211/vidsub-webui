"""字幕质量量化（09 号票）：与人工听写的参考字幕做词级比对

为什么要单独测这一套：WER 这类指标最容易**看起来算对了其实没有**。
之前就踩过 —— 参考字幕是滚动格式没去重，词数 3 倍冗余，WER 虚高到
69.98%，差点当成结论汇报。所以这里把三件事分别钉住：

1. **解析**：双语 SRT 里要能只取英文行（中文行混进去词数就全错）
2. **裁剪**：参考字幕只覆盖素材的一部分时，多出来的转写不能算成插入错误
3. **分类**：差异要能区分"分段边界造成的重复/缺失"和"真的听错" ——
   只报一个总数会误导，实测里前者占了差异的大头
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "tools"))

import check_quality as cq       # noqa: E402


BILINGUAL = """1
00:00:01,000 --> 00:00:03,000
大家请思考一下自己最大的目标。
Everyone, please think of your biggest goal.

2
00:00:03,500 --> 00:00:06,000
真的，花点时间吧。
For real, take a second.

"""

PLAIN = """1
00:00:01,000 --> 00:00:03,000
Everyone, please think
of your biggest goal.

2
00:00:03,500 --> 00:00:06,000
For real, take a second.

"""


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


# --- 解析 ---------------------------------------------------------------

def test_parse_bilingual_srt_keeps_only_english(tmp_path):
    """双语 SRT 的 cue 里中文在上英文在下；只取英文时中文行必须被丢掉。

    混进中文会让"词数比"完全失真 —— 中文按空格切出来的"词"没有意义。
    """
    cues = cq.parse_srt(_write(tmp_path, "bi.srt", BILINGUAL), english_only=True)

    assert len(cues) == 2
    assert cues[0][2] == "Everyone, please think of your biggest goal."
    assert "思考" not in cues[0][2]


def test_parse_keeps_time_span(tmp_path):
    """时间轴要一起解析出来 —— 裁剪和基准统计都靠它"""
    cues = cq.parse_srt(_write(tmp_path, "bi.srt", BILINGUAL), english_only=True)
    assert cues[0][0] == pytest.approx(1.0)
    assert cues[0][1] == pytest.approx(3.0)
    assert cues[1][1] == pytest.approx(6.0)


def test_parse_plain_srt_joins_wrapped_lines(tmp_path):
    """官方 SRT 一条 cue 常折成两行，要接成一句而不是当成两条"""
    cues = cq.parse_srt(_write(tmp_path, "en.srt", PLAIN), english_only=True)
    assert cues[0][2] == "Everyone, please think of your biggest goal."


def test_parse_ignores_blank_and_index_lines(tmp_path):
    cues = cq.parse_srt(_write(tmp_path, "en.srt", PLAIN), english_only=True)
    assert all(c[2].strip() for c in cues)


# --- 归一化 -------------------------------------------------------------

def test_norm_words_strips_punctuation_and_case():
    assert cq.norm_words("Everyone, PLEASE think!") == ["everyone", "please", "think"]


def test_norm_words_splits_contractions():
    """you've / youve 要归一到同一个词，否则每个缩写都算一次错误。

    参考字幕写 "You've"，识别输出常写 "youve" 或 "you" —— 不归一的话
    这类差异会污染 WER，而它们不是听错。
    """
    assert cq.norm_words("You've got") == cq.norm_words("youve got")
    assert cq.norm_words("don't") == cq.norm_words("dont")


def test_norm_words_handles_unicode_dashes_and_quotes():
    """官方字幕用 -- 当破折号、用弯引号，不归一就会粘出奇怪的词"""
    assert cq.norm_words("For real -- you can") == ["for", "real", "you", "can"]
    assert cq.norm_words("it\u2019s") == ["its"]


# --- WER ---------------------------------------------------------------

def test_wer_counts_substitution_over_reference_length():
    """手算例子：ref 4 词、1 处替换 → 25%

    期望值来自定义（编辑距离 / 参考词数），不是用实现重算一遍。
    """
    ref = cq.norm_words("alpha beta gamma delta")
    hyp = cq.norm_words("alpha xxxxx gamma delta")
    assert cq.wer_of(ref, hyp) == pytest.approx(0.25)


def test_wer_is_zero_for_identical():
    ref = cq.norm_words("one two three")
    assert cq.wer_of(ref, list(ref)) == 0.0


def test_wer_counts_deletion_and_insertion():
    ref = cq.norm_words("one two three four")
    # 少一个词、多一个词 → 2 处错误 / 4 参考词
    hyp = cq.norm_words("one three four five")
    assert cq.wer_of(ref, hyp) == pytest.approx(0.5)


def test_word_ratio_gate_flags_implausible_comparison():
    """词数比是**先决条件**：比值离谱时 WER 没有意义，必须被标出来。

    这条是拿血换来的：滚动字幕没去重导致词数 3 倍冗余，WER 虚高到 69.98%，
    差点当成结论汇报。工具必须自己喊出来，而不是等人去算。
    """
    ref = cq.norm_words(" ".join(["word"] * 100))
    hyp = cq.norm_words(" ".join(["word"] * 300))

    ratio = cq.word_ratio(ref, hyp)
    assert ratio == pytest.approx(3.0)
    assert not cq.ratio_is_plausible(ratio), "3 倍冗余竟然被判为可信"


def test_word_ratio_accepts_near_one():
    ref = cq.norm_words(" ".join(["word"] * 100))
    hyp = cq.norm_words(" ".join(["word"] * 104))
    assert cq.ratio_is_plausible(cq.word_ratio(ref, hyp))


# --- 按参考覆盖范围裁剪 --------------------------------------------------

def test_restrict_drops_cues_beyond_reference_end():
    """参考字幕只覆盖到 3:10，之后那段（赞助片头）不算错。

    素材尾部有参考字幕没覆盖的内容时，多出来的转写会被算成**插入错误**，
    把 WER 抬高。裁剪掉才是在比"同一段内容"。
    """
    cues = [(0.0, 5.0, "hello there"), (5.0, 10.0, "world again"),
            (200.0, 210.0, "sponsors of tomorrow")]
    kept, dropped = cq.restrict_to(cues, until=10.0)

    assert [c[2] for c in kept] == ["hello there", "world again"]
    assert dropped == 1


def test_restrict_keeps_cue_that_starts_before_cutoff():
    """跨过裁剪点的 cue 要留下（只按起点判），不能把半句砍掉"""
    cues = [(0.0, 12.0, "starts before ends after")]
    kept, dropped = cq.restrict_to(cues, until=10.0)
    assert len(kept) == 1
    assert dropped == 0


# --- 差异分类 -----------------------------------------------------------

def _ops(ref_text, hyp_text):
    ref = cq.norm_words(ref_text)
    hyp = cq.norm_words(hyp_text)
    return ref, hyp, cq.diff_ops(ref, hyp)


def test_classify_spelling_variant():
    """afterward / afterwards 是词形差异，不是听错"""
    ref, hyp, ops = _ops("we went home afterward", "we went home afterwards")
    cats = cq.classify_diffs(ref, hyp, ops)
    assert "词形差异" in cats


def test_classify_segment_boundary_duplication():
    """边界重复：识别按自己的段边界切，边界词被相邻两条各带一次。

    这里 ref 只有一次 "the"，hyp 在中间多出一个 "the"，而它紧邻的
    上下文里就有 "the" —— 典型的切分抖动，不是听错。
    """
    ref, hyp, ops = _ops("we saw the big house today",
                         "we saw the the big house today")
    cats = cq.classify_diffs(ref, hyp, ops)
    assert "分段边界重复/缺失" in cats


def test_classify_number_writing():
    """数字写法：官方写 329，转写写 three two nine —— 不是听错"""
    ref, hyp, ops = _ops("there were 329 people", "there were three two nine people")
    cats = cq.classify_diffs(ref, hyp, ops)
    assert "数字/缩写写法" in cats


def test_classify_real_mishearing():
    """真正听错：换成完全不相干的词，且不在边界上"""
    ref, hyp, ops = _ops("she bought a lemon", "she bought a melon")
    cats = cq.classify_diffs(ref, hyp, ops)
    assert "疑似真实听错" in cats


def test_classify_official_non_speech_marker():
    """官方字幕把 `(Laughter)` 写进正文，我们不该转写它 —— 这是官方噪声。

    不单独摘出来的话，每处笑声/掌声都算一次"漏词"，WER 被抬高，而那是
    参考自己的标注习惯，不是识别质量问题。工单要求的"官方字幕自身噪声
     vs 真实听错"就是这一条。
    """
    ref, hyp, ops = _ops("that was funny (Laughter) and then we left",
                         "that was funny and then we left")
    cats = cq.classify_diffs(ref, hyp, ops)
    assert "参考非语音标记" in cats
    assert "疑似真实听错" not in cats


def test_classify_tokenization_difference():
    """`any time` / `anytime` 字母完全一样，只是分词边界不同 —— 不是听错。

    只按单词逐个比会把这类全算成听错；先看拼接结果是否相同就能摘出来。
    """
    ref, hyp, ops = _ops("come back any time you like",
                         "come back anytime you like")
    cats = cq.classify_diffs(ref, hyp, ops)
    assert "词形差异" in cats
    assert "疑似真实听错" not in cats


def test_classify_accounts_for_every_difference():
    """每处差异都必须被归到某一类，不能悄悄漏掉

    漏掉的话"分类占比"加起来不到 100%，而报告里没人会去核对。
    """
    ref, hyp, ops = _ops(
        "we went home afterward and there were 329 people",
        "we went home afterwards and there were three two nine people")
    cats = cq.classify_diffs(ref, hyp, ops)

    total = sum(v["count"] for v in cats.values())
    assert total == sum(1 for o in ops if o[0] != "equal")
