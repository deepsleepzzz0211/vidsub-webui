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

import re
from typing import Optional

MAX_ZH = 34
MAX_WORDS = 18

_LINE = re.compile(r"[\u4e00-\u9fff]")


def _is_zh(text: str) -> bool:
    return bool(_LINE.search(text))


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


def format_srt(cues: list, style: str = "bilingual", max_zh: int = MAX_ZH,
               max_words: int = MAX_WORDS) -> str:
    """把 cue 列表渲染成 SRT 文本。style ∈ bilingual / mono / en。"""
    splited = split_cues(cues, max_zh=max_zh, max_words=max_words)
    lines: list[str] = []
    n = 0
    for start, end, src, dst in splited:
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


def _has_zh(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def _ts(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")