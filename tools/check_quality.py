"""字幕质量量化（09 号票）：与**人工听写**的参考字幕做词级比对

为什么参考字幕必须挑人工听写的：官方**自动**字幕自己就错得不少（实测把
GitHub 写成 `gt gt`、把域名拆成 `dot com`，约 22% 的"差异"来自它自己），
拿它当 Ground Truth 等于拿一把不准的尺子量东西。

⚠️ **先看词数比，再看 WER**。参考词数与转写词数之比接近 1.0，才说明两边
覆盖的是同一段内容，那个百分比才有意义。这条是拿血换来的：滚动字幕没去重
导致词数 3 倍冗余，WER 虚高到 69.98%，差点当成结论汇报。所以本工具把比值
放在最前面，并会主动判定它是否可信。

与 `yt2sub/wer.py` 的区别：那个是给 YouTube 滚动 VTT 写的，需要"最长后缀
重叠"去重（每个 cue 重复上一 cue 的全部文本）。这里的参考是干净的 SRT，
不需要那套 —— 但那套工具**保留在原处**，因为它的场景仍然存在。

用法：
    python tools/check_quality.py <我们的srt> <参考srt> [--json out.json]

退出码：0 正常；1 词数比不可信（此时 WER 无意义，别拿它当结论）。
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys

CJK = re.compile(r"[\u4e00-\u9fff]")
_TIME = r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})"
CUE_RE = re.compile(rf"^{_TIME}\s*-->\s*{_TIME}")

# 词数比的可信区间。超出说明两边覆盖的不是同一段内容 —— 参考只覆盖了
# 素材的一部分、参考是滚动格式没去重、或者语言选错了。三种都不是"质量差"，
# 而是"这次比对不成立"。
RATIO_LO, RATIO_HI = 0.85, 1.15

_QUOTES = ("\u2018", "\u2019", "\u201c", "\u201d", "\u2032")
_DASHES = ("\u2014", "\u2013", "\u2012", "\u2015", "\u2026", "\u200b")

_NUMBER_WORDS = {
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty",
    "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred",
    "thousand", "million", "billion",
}

# 官方字幕用来标注**非语音**的记号（`(Laughter)`、`(Applause)` …）。
# 它们不是识别错误：我们本来就不该把掌声笑声转写成文字，但官方字幕把它们
# 写进了正文，于是比对时全成了"漏词"。这正是工单说的"官方字幕自身噪声"，
# 必须单独摘出来，否则会算进真实听错里。
_NONSPEECH = {
    "laughter", "laughs", "laughing", "applause", "applauding", "music",
    "cheering", "cheers", "inaudible", "crosstalk", "sighs", "coughs",
}


def _to_sec(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_srt(path: str, english_only: bool = False):
    """解析 SRT → `[(起始秒, 结束秒, 文本)]`。

    `english_only`：双语字幕里只留非中文行。中文按空格切词没有意义，
    混进来会让词数比完全失真。

    一条 cue 折成多行时要接成一句 —— 官方字幕按阅读断行，不是按语义。
    """
    cues = []
    with open(path, encoding="utf-8") as f:
        text = f.read()

    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [l for l in block.strip().splitlines() if l.strip()]
        if len(lines) < 2:
            continue

        span, body_start = None, 0
        for i, line in enumerate(lines[:3]):
            m = CUE_RE.search(line)
            if m:
                g = m.groups()
                span = (_to_sec(*g[0:4]), _to_sec(*g[4:8]))
                body_start = i + 1
                break
        if span is None:
            continue

        body = lines[body_start:]
        if english_only:
            body = [l for l in body if not CJK.search(l)]
        txt = " ".join(body).strip()
        if txt:
            cues.append((span[0], span[1], txt))
    return cues


def norm_words(text: str) -> list:
    """归一化成可比较的词序列。

    撇号直接去掉（`you've` → `youve`）：参考字幕写 "You've"、识别常输出
    "youve"，不归一的话每个缩写都算一次错误，而它们不是听错。
    """
    s = text.lower()
    for q in _QUOTES:
        s = s.replace(q, "'")
    for d in _DASHES:
        s = s.replace(d, " ")
    s = s.replace("--", " ")
    s = re.sub(r"[^a-z0-9']+", " ", s)
    s = s.replace("'", "")
    return s.split()


def diff_ops(ref: list, hyp: list):
    """difflib 的编辑操作序列。抽成函数是为了测试能直接构造 opcodes。"""
    return difflib.SequenceMatcher(a=ref, b=hyp, autojunk=False).get_opcodes()


def _op_cost(op) -> int:
    """一处编辑操作算几个错误。

    **必须取两侧的最大值，不能只取参考侧**：`insert` 操作的参考侧长度恒为 0，
    只累加 `i2 - i1` 会把所有"多出来的词"漏掉，WER 系统性偏低。
    （`yt2sub/wer.py` 就是那么写的，实测少算了插入。）
    替换取 1、纯删 1、纯插 1 —— 与编辑距离的定义一致。
    """
    _, i1, i2, j1, j2 = op
    return max(i2 - i1, j2 - j1)


def wer_of(ref: list, hyp: list) -> float:
    """词错率 = 编辑距离 / 参考词数。参考为空时返回 0。"""
    if not ref:
        return 0.0
    errs = sum(_op_cost(o) for o in diff_ops(ref, hyp) if o[0] != "equal")
    return errs / len(ref)


def word_ratio(ref: list, hyp: list) -> float:
    return len(hyp) / len(ref) if ref else 0.0


def ratio_is_plausible(ratio: float, lo: float = RATIO_LO,
                       hi: float = RATIO_HI) -> bool:
    """词数比是否落在可信区间。不落在里面时 WER 没有意义。"""
    return lo <= ratio <= hi


def restrict_to(cues: list, until: float):
    """按参考字幕的覆盖范围裁剪，返回 `(保留的, 丢掉几条)`。

    素材尾部可能有参考字幕没覆盖的内容（实测那段 TED 素材后面还挂了个
    赞助片头）。不裁的话那些转写会被算成**插入错误**，把 WER 抬高。

    只按**起点**判：跨过裁剪点的 cue 整条保留，不能把半句砍掉。
    """
    kept = [c for c in cues if c[0] < until]
    return kept, len(cues) - len(kept)


def _neighbour_words(ops, idx: int, ref: list, ctx: int = 4) -> set:
    """紧邻差异的 equal 块里、贴近边界的那几个词。

    只取**贴边界**的一小段，不取整个 equal 块：边界抖动的特征是被重复/漏掉的
    词就出现在差异旁边，取整块会让判定松到几乎什么都算边界问题。
    """
    words = set()
    if idx - 1 >= 0 and ops[idx - 1][0] == "equal":
        _, i1, i2, _, _ = ops[idx - 1]
        words.update(ref[max(0, i2 - ctx):i2])
    if idx + 1 < len(ops) and ops[idx + 1][0] == "equal":
        _, i1, i2, _, _ = ops[idx + 1]
        words.update(ref[i1:i1 + ctx])
    return words


def _is_spelling_variant(r: list, h: list) -> bool:
    """写法差异（不是听错）：词形微调，或同一个词被切成了不同份数。

    - 词形：`afterward`/`afterwards`、`acknowledgment`/`acknowledgement`
    - 分词：`any time`/`anytime`、`you've`/`youve` —— 字母完全一样，只是
      分词边界不同。只比单词会把这类全算成听错。
    """
    if "".join(r) == "".join(h) and r != h:
        return True
    if len(r) != 1 or len(h) != 1:
        return False
    a, b = r[0], h[0]
    if a == b:
        return False
    if len(a) >= 4 and (a.startswith(b) or b.startswith(a)):
        return True
    return (abs(len(a) - len(b)) <= 2
            and difflib.SequenceMatcher(None, a, b).ratio() >= 0.85)


def _has_number(words) -> bool:
    return any(w.isdigit() or w in _NUMBER_WORDS for w in words)


def _label(tag: str, r: list, h: list, ops, idx: int, ref: list) -> str:
    """给一处差异归类。

    优先级是有讲究的：先摘掉**不属于识别质量**的两类（官方字幕的非语音
    标记、写法差异），再看纯增删是不是边界抖动，剩下的才叫"疑似真实听错"。
    顺序反了会把官方噪声算成我们的错，得出一个虚高的 WER。
    """
    # 官方标注的非语音记号（(Laughter)/(Applause)）—— 我们不该转写它们
    if any(w in _NONSPEECH for w in r):
        return "参考非语音标记"

    if tag == "replace":
        if _is_spelling_variant(r, h):
            return "词形差异"
        if _has_number(r) or _has_number(h):
            return "数字/缩写写法"
        return "疑似真实听错"

    # insert / delete
    if _has_number(r) or _has_number(h):
        return "数字/缩写写法"
    extra = set(h) if tag == "insert" else set(r)
    if extra and extra <= _neighbour_words(ops, idx, ref):
        return "分段边界重复/缺失"
    return "疑似真实听错"


def classify_diffs(ref: list, hyp: list, ops=None) -> dict:
    """把差异分类。返回 `{类别: {"count", "cost", "examples"}}`。

    `cost` 用 `max(参考词数, 转写词数)` 加权 —— 一处"替换 1 个词"和一处
    "替换 5 个词"不该算同等份量。占比按 cost 算。

    **每处差异都必须落到某一类**，不能漏：漏掉的话占比加起来不到 100%，
    而报告里没人会去核对。
    """
    ops = ops if ops is not None else diff_ops(ref, hyp)
    cats: dict = {}
    for idx, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            continue
        r, h = ref[i1:i2], hyp[j1:j2]
        if not r and not h:
            continue
        cat = _label(tag, r, h, ops, idx, ref)
        cost = max(len(r), len(h))
        slot = cats.setdefault(cat, {"count": 0, "cost": 0, "examples": []})
        slot["count"] += 1
        slot["cost"] += cost
        if len(slot["examples"]) < 6:
            slot["examples"].append(
                {"op": tag, "ref": " ".join(r)[:60], "hyp": " ".join(h)[:60]})
    return cats


def compare(ours: str, reference: str, english_only: bool = True) -> dict:
    """跑完整比对，返回可序列化的报告。"""
    ours_cues = parse_srt(ours, english_only=english_only)
    ref_cues = parse_srt(reference, english_only=False)

    if not ref_cues:
        raise SystemExit(f"参考字幕解析出 0 条 cue，检查文件：{reference}")
    if not ours_cues:
        raise SystemExit(f"待评字幕解析出 0 条 cue，检查文件：{ours}")

    # 参考只覆盖到它末条 cue 的结束时间；超出部分不算错
    ref_end = max(c[1] for c in ref_cues)
    kept, dropped = restrict_to(ours_cues, until=ref_end)

    ref = [w for _, _, t in ref_cues for w in norm_words(t)]
    hyp = [w for _, _, t in kept for w in norm_words(t)]

    ops = diff_ops(ref, hyp)
    errs = sum(_op_cost(o) for o in ops if o[0] != "equal")
    ratio = word_ratio(ref, hyp)

    return {
        "ours": ours,
        "reference": reference,
        "baseline": {
            "ours_cues": len(ours_cues),
            "ours_cues_compared": len(kept),
            "ours_cues_dropped": dropped,
            "ours_end_seconds": round(max(c[1] for c in ours_cues), 3),
            "ref_cues": len(ref_cues),
            "ref_end_seconds": round(ref_end, 3),
        },
        "words": {"ref": len(ref), "hyp": len(hyp), "ratio": round(ratio, 4),
                  "plausible": ratio_is_plausible(ratio)},
        "wer": {"errors": errs, "rate": round(errs / len(ref), 5),
                "accuracy": round(1 - errs / len(ref), 5)},
        "categories": classify_diffs(ref, hyp, ops),
    }


def _print_report(r: dict) -> None:
    b, w, wer = r["baseline"], r["words"], r["wer"]
    print(f"待评字幕  {r['ours']}")
    print(f"参考字幕  {r['reference']}")
    print()
    print("── 基准 ──")
    print(f"  我们的 cue        {b['ours_cues']} 条（比对 {b['ours_cues_compared']} 条，"
          f"裁掉 {b['ours_cues_dropped']} 条）")
    print(f"  我们的覆盖        到 {b['ours_end_seconds']:.1f}s")
    print(f"  参考 cue          {b['ref_cues']} 条，覆盖到 {b['ref_end_seconds']:.1f}s")
    print()
    print("── 词数比（先看这个）──")
    print(f"  参考 {w['ref']} 词   转写 {w['hyp']} 词   比值 {w['ratio']:.3f}")
    if not w["plausible"]:
        print("  ⚠️ 比值超出可信区间 —— 两边覆盖的不是同一段内容，"
              "下面的 WER 没有意义")
    print()
    print("── WER ──")
    print(f"  编辑距离 {wer['errors']}   WER {wer['rate']*100:.2f}%   "
          f"准确率 {wer['accuracy']*100:.2f}%")
    print()
    print("── 差异分类（按影响词数排序）──")
    cats = r["categories"]
    total = sum(v["cost"] for v in cats.values()) or 1
    for name, v in sorted(cats.items(), key=lambda x: -x[1]["cost"]):
        print(f"  {name:16s} {v['count']:3d} 处  ≈{v['cost']:4d} 词  "
              f"占差异 {v['cost']/total*100:5.1f}%")
        for ex in v["examples"][:3]:
            print(f"      参考 {ex['ref']!r}")
            print(f"      转写 {ex['hyp']!r}")
    print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="字幕质量量化（英文词错率）")
    ap.add_argument("ours", help="待评字幕（可以是双语 SRT）")
    ap.add_argument("reference", help="参考字幕（人工听写的英文 SRT）")
    ap.add_argument("--all-lines", action="store_true",
                    help="不按英文过滤（参考本身含中文时用）")
    ap.add_argument("--json", help="把报告写成 JSON")
    args = ap.parse_args(argv)

    report = compare(args.ours, args.reference,
                     english_only=not args.all_lines)
    _print_report(report)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"JSON 已写出：{args.json}")

    # 词数比不可信时用退出码喊出来 —— 这时候的 WER 不能当结论用
    return 0 if report["words"]["plausible"] else 1


if __name__ == "__main__":
    sys.exit(main())
