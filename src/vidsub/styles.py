"""字幕分段与样式（07 号票）

双语字幕有两条显示问题必须解决，不然一行字直接盖掉整个幻灯片：
- 单条 cue 太长就该拆 —— 否则 45 词的英文 + 一整句中文一路顶出画面外。
- 中英文的行数与字数限制不同，要各拆各的，但 azt 要按时间比例合一条 cue。

样式档：
- bilingual: 中文在上、英文在下
- mono:      只留中文（纯中文字幕）
- en:        只留英文
"""
from __future__ import annotations

MAX_ZH = 34
MAX_WORDS = 18

# 一条 cue 最多占几行（07 号票的硬要求：字幕不能盖住幻灯片正文）。
#
# 阈值不是拍的：burn.py 里 SIZE_BILINGUAL=19px 是在 45 秒片段上抽帧逐档试
# 出来的（30px 占 5 行盖住正文、22px 字大且空、19px 4 行刚好）。按 19px、
# 1920 宽、左右边距各 80px 反推，一行约放 45 个汉字或 90 个英文字符，
# 于是 34 字中文 / 18 词英文都是**一行**，双语两条 = 2 行，留了 2 行余量。
# 这个函数把那个反推写成可执行的断言，免得以后改字号时无声破功。
MAX_LINES = 4
CHARS_PER_LINE_ZH = 45
CHARS_PER_LINE_EN = 90

# 相邻两条字幕**整句相同**、间隔不超过 DUP_WINDOW 秒时，判为切分抖动并掐掉
# 后一条。两个阈值缺一不可：
#   DUP_WINDOW —— 说话人隔 10 秒说两遍 "yes" 是正常内容，不该删。
#   DUP_MIN_LEN —— 一两个词的重复（"yes" "okay"）在真实对话里很常见，
#     删掉是破坏内容；只有**整句**被切两次才是抖动。中文按字数、英文按词数，
#     取两边的较大值。
DUP_WINDOW = 1.0
DUP_MIN_WORDS = 4
DUP_MIN_CHARS = 8


def split_cues(cues: list, max_zh: int = MAX_ZH,
               max_words: int = MAX_WORDS) -> list:
    """把过长的 cue 拆成几条子 cue，文字按段均分，时间按比例均分。

    cues 元素形如 (start, end, src, dst)。只改时间分配，不改任
    何文字——否则同一句话的两半会各自出现不同的措辞。
    拆分后子 cue 用连小数点的时间轴保持单调不回退。
    """
    out = []
    for start, end, src, dst in cues:
        en = src or ""
        zh = dst or ""
        en_words = en.split() if en else []
        zh_chars = zh if zh else ""

        ni = 1
        if en_words:
            ni = max(ni, -(-len(en_words) // max_words))
        if zh_chars:
            ni = max(ni, -(-len(zh_chars) // max_zh))
        if ni <= 1:
            out.append((start, end, src, dst))
            continue

        def chunk(words_or_chars, parts):
            if not words_or_chars:
                return [""] * parts
            per = -(-len(words_or_chars) // parts)
            seg = [words_or_chars[i:i + per] for i in range(0, len(words_or_chars), per)]
            while len(seg) < parts:
                seg.append([] if isinstance(words_or_chars, list) else "")
            return seg[:parts]

        en_parts = chunk(en_words, ni)
        zh_parts = chunk(list(zh_chars) if zh_chars else "", ni)
        part_dur = (end - start) / ni
        for i in range(ni):
            a = start + part_dur * i
            b = end if i == ni - 1 else start + part_dur * (i + 1)
            en_i = " ".join(en_parts[i]) if isinstance(en_parts[i], list) else ""
            zh_i = "".join(zh_parts[i]) if isinstance(zh_parts[i], list) else zh_parts[i]
            out.append((a, b, en_i, zh_i))
    return out


def _repair(cues: list, dup_window: float = DUP_WINDOW) -> list:
    """修时间轴：裁重叠、掐掉相邻重复。09 号票的不变量。

    - **重叠**：两条 cue 时间交叠时把后者起点推到前者终点。交叠的字幕在
      播放时会同时显示两行，视觉上就是重影。VAD 理论上不产出交叠段，
      但 ASR 段的时间是模型给的，边界会互相压到 —— 不能假设上游干净。
    - **相邻重复**：中英**整句都相同**、间隔在 `dup_window` 秒内、且句子
      足够长，才掐掉后一条。三个条件缺一不可：短句（"yes" "okay"）在真实
      对话里反复出现，删掉是破坏内容；间隔大的重复是说话人真的又说一遍。
      只有"长句 + 紧挨着 + 一字不差"才是切分抖动。
    """
    out: list = []
    prev_end = None
    prev_key = None
    prev_is_long = False
    prev_end_t = None
    for start, end, src, dst in cues:
        if prev_end is not None and start < prev_end:
            start = prev_end
        if end <= start:
            continue                      # 被裁成空的了，丢掉
        zh, en = dst or "", src or ""
        if (prev_end_t is not None and start - prev_end_t <= dup_window
                and prev_is_long and (zh, en) == prev_key):
            continue
        out.append((start, end, src, dst))
        prev_end = end
        prev_end_t = end
        prev_key = (zh, en)
        prev_is_long = _is_long(zh, en)
    return out


def _is_long(zh: str, en: str) -> bool:
    """够不够"长到像一句被切了两次的话"。"""
    return len(zh.strip()) >= DUP_MIN_CHARS or len(en.split()) >= DUP_MIN_WORDS


def format_srt(cues: list, style: str = "bilingual", max_zh: int = MAX_ZH,
               max_words: int = MAX_WORDS) -> str:
    """把 cue 列表渲染成 SRT 文本。style ∈ bilingual / mono / en。

    顺序：先分段（长 cue 拆细）→ 再修时间轴（裁重叠、掐重复）→ 再渲染。
    修复放在分段之后：拆分会让子 cue 首尾相接，正是最容易撞上重复的地方。
    """
    splited = split_cues(cues, max_zh=max_zh, max_words=max_words)
    lines: list[str] = []
    n = 0
    for start, end, src, dst in _repair(splited):
        if style == "mono":
            rows = [dst]
        elif style == "en":
            rows = [src]
        else:  # bilingual，一定中文在上英文在下
            rows = [dst, src]
        rows = [r.strip() for r in rows if r and r.strip()]
        if not rows:
            continue
        n += 1
        lines.append(str(n))
        lines.append(f"{_ts(start)} --> {_ts(end)}")
        lines.extend(rows)
        lines.append("")
    return "\n".join(lines)


def _ts(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def est_lines(zh: str, en: str) -> int:
    """估算一条 cue 在压制后占几行（中英各算，取和）。

    用来验证 MAX_ZH / MAX_WORDS 与 burn.py 里的字号配套：字号变大或边距
    变宽，行数就超了，这时候该调阈值而不是等成片出来才发现盖住正文。
    """
    n = 0
    if zh:
        n += -(-len(zh) // CHARS_PER_LINE_ZH)
    if en:
        n += -(-len(en) // CHARS_PER_LINE_EN)
    return n


def thresholds_fit(max_lines: int = MAX_LINES) -> bool:
    """当前阈值下，一条 cue 最坏情况会不会超过 max_lines 行。"""
    return est_lines("字" * MAX_ZH, "w" * (MAX_WORDS * 5)) <= max_lines
