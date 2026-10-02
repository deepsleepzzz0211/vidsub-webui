"""字幕分段与样式（07）

- 长 cue 必须拆，否则一整条字盖掉整个幻灯片
- 双语 SRT：中文在上、英文在下；mono 只留中文；en 只留英文
- 拆分只改时间分配，不改文字
- 09 号票要求的全局不变量：时间轴零倒退、零重叠、零重复
"""
import re

from vidsub import styles

_TS = re.compile(r"^(\d+):(\d+):(\d+),(\d+)$")


def _parse(srt: str) -> list:
    """把 SRT 文本解成 [(start, end, [行...])]，用来验全局不变量。"""
    out = []
    for block in srt.strip().split("\n\n"):
        lines = [l for l in block.strip().splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        a, b = lines[1].split("-->")
        out.append((_secs(a), _secs(b), lines[2:]))
    return out


def _secs(ts: str) -> float:
    m = _TS.match(ts.strip())
    assert m, f"时间戳格式不对：{ts!r}"
    h, mi, s, ms = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000.0


def test_short_cue_not_split():
    cues = [(0.0, 2.0, "hello world", "你好")]
    assert styles.split_cues(cues) == cues


def test_long_english_cue_split_by_words():
    en = " ".join(["word"] * 40)  # 40 词，超 18 字
    out = styles.split_cues([(0.0, 4.0, en, "你好" * 10)], max_words=18)
    assert len(out) == 3          # ceil(40/18)=3
    # 总时长 + 起点 + 终点不变
    assert out[0][0] == 0.0
    assert abs(out[-1][1] - 4.0) < 1e-6
    # monotonic
    for i in range(1, len(out)):
        assert out[i][0] >= out[i - 1][1] - 1e-6


def test_long_chinese_cue_split_by_chars():
    zh = "字" * 80
    out = styles.split_cues([(0.0, 4.0, "hi", zh)], max_zh=34)
    assert len(out) == 3
    assert out[0][0] == 0.0 and abs(out[-1][1] - 4.0) < 1e-6


def test_split_keeps_text_together_no_loss():
    en = " ".join(["w%d" % i for i in range(20)])
    out = styles.split_cues([(1.0, 3.0, en, "中" * 40)], max_words=18, max_zh=34)
    joined = " ".join(w for (_,_,s,_) in out for w in s.split())
    assert joined == en


def test_bilingual_has_chinese_first():
    srt = styles.format_srt([(0.0, 1.0, "hello", "你好")], style="bilingual")
    assert srt.index("你好") < srt.index("hello")


def test_mono_keeps_chinese_only():
    srt = styles.format_srt([(0.0, 1.0, "hello", "你好")], style="mono")
    assert "你好" in srt and "hello" not in srt


def test_en_keeps_english_only():
    srt = styles.format_srt([(0.0, 1.0, "hello", "你好")], style="en")
    assert "hello" in srt and "你好" not in srt


def test_format_skips_empty():
    srt = styles.format_srt([(0.0, 1.0, "", ""), (1.2, 2.0, "ok", "好")])
    lines = [l for l in srt.splitlines() if l.strip()]
    assert lines[0] == "1"


def test_ts_format():
    assert styles._ts(3723.5) == "01:02:03,500"


# --- 09 号票：全局时间轴不变量 ---------------------------------------------
#
# 这些必须在**渲染出来的 SRT 文本**上验，而不是在 split_cues 的返回值上。
# 之前的用例只看单条 cue 内部，跨 cue 的倒退/重叠/重复一个都没抓到。

def _no_spaces_cues(n: int) -> list:
    """造 n 条互不重叠、单调递增的 cue。"""
    return [(i * 2.0, i * 2.0 + 1.8, f"line number {i}", f"第{i}行") for i in range(n)]


def test_timeline_never_goes_backwards():
    cues = _no_spaces_cues(50)
    parsed = _parse(styles.format_srt(cues))
    prev_end = -1.0
    for start, end, _ in parsed:
        assert start >= prev_end - 0.001, f"时间轴倒退了：{start} < {prev_end}"
        prev_end = end


def test_no_overlap_between_cues():
    cues = [(0.0, 2.0, "a", "甲"), (1.5, 3.0, "b", "乙"),
            (2.8, 4.0, "c", "丙")]
    parsed = _parse(styles.format_srt(cues))
    for (a1, b1, _), (a2, _, _) in zip(parsed, parsed[1:]):
        assert a2 >= b1 - 0.001, f"相邻 cue 重叠：{b1} > {a2}"


def test_no_internal_duplicate_text():
    """相邻两条不能是同一句长话 —— 重复字幕在播放时非常刺眼。

    句子要够长才算"该被去重"：短句重复（yes/okay）是真实对话内容。
    """
    cues = [(0.0, 1.5, "this whole sentence is repeated verbatim",
             "这一整句话被原样重复了一遍"),
            (1.6, 3.1, "this whole sentence is repeated verbatim",
             "这一整句话被原样重复了一遍")]
    parsed = _parse(styles.format_srt(cues))
    texts = [" ".join(t) for _, _, t in parsed]
    assert len(set(texts)) == len(texts), f"长句被切了两次没去重：{texts}"


def test_no_duplicate_after_splitting():
    """拆分之后也不能造出两条一模一样的长 cue。

    这是真实会发生的事：VAD 把同一句话切成了两段，翻译给出相同译文。
    """
    cues = [(0.0, 2.0, "the very same long sentence appears twice in a row",
             "完全相同的一整句长话连续出现两次"),
            (2.1, 4.1, "the very same long sentence appears twice in a row",
             "完全相同的一整句长话连续出现两次")]
    parsed = _parse(styles.format_srt(cues))
    texts = [" ".join(t) for _, _, t in parsed]
    assert len(set(texts)) == len(texts), f"拆分后出现重复：{texts}"


def test_genuine_repetition_is_kept():
    """真实的重复要保留 —— 去重不能变成"把所有重复都吞了"。

    两种都验：隔很久的长句重复、紧挨着的短句重复（yes / okay 在真实
    对话里反复出现，删掉就是破坏内容）。
    """
    far_apart = [(0.0, 1.5, "a long sentence said once here",
                  "这里说了一句很长的话"),
                 (10.0, 11.5, "a long sentence said once here",
                  "这里说了一句很长的话")]
    assert len(_parse(styles.format_srt(far_apart))) == 2, "远距重复被误删了"

    short_repeat = [(0.0, 1.0, "yes", "对"), (1.1, 2.1, "yes", "对")]
    assert len(_parse(styles.format_srt(short_repeat))) == 2, "短句重复被误删了"


def test_numbering_is_contiguous_after_splitting():
    """长 cue 拆开后序号必须连续 —— 断号会让部分播放器跳过字幕。"""
    en = " ".join(["word"] * 90)      # 会拆成 5 段
    zh = "字" * 170
    srt = styles.format_srt([(0.0, 5.0, en, zh), (5.0, 6.0, "ok", "好")])
    nums = [int(b.strip().splitlines()[0])
            for b in srt.strip().split("\n\n") if b.strip()]
    assert nums == list(range(1, len(nums) + 1)), nums


def test_long_mixed_video_timeline_is_sane():
    """一整轮真实规模的 cue（200 条 + 长句拆分）走完，轴仍然干净。"""
    cues = []
    t = 0.0
    for i in range(200):
        en = " ".join(f"w{i}_{j}" for j in range(20 + (i % 25)))
        zh = f"第{i}句" + "内容" * (i % 18)
        cues.append((t, t + 2.4, en, zh))
        t += 2.5
    parsed = _parse(styles.format_srt(cues))
    assert len(parsed) >= 200, "拆分后条目反而变少了"
    prev_end = -1.0
    seen = set()
    for start, end, lines in parsed:
        assert start >= prev_end - 0.001, f"时间轴倒退：{start} < {prev_end}"
        assert end > start, f"cue 时长非正：{start}→{end}"
        key = " ".join(lines)
        assert key not in seen, f"出现重复字幕：{key}"
        seen.add(key)
        prev_end = end
    assert abs(prev_end - cues[-1][1]) < 0.01, "总时长被改动了"