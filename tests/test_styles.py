"""字幕分段与样式（07）

- 长 cue 必须拆，否则一整条字盖掉整个幻灯片
- 双语 SRT：中文在上、英文在下；mono 只留中文；en 只留英文
- 拆分只改时间分配，不改文字
"""
from vidsub import styles


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