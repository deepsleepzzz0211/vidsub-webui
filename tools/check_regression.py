"""回归与基线（09 号票）

同一视频、同样工具链，产出的字幕必须字节级一致。本工具把 pipeline 对同一
视频跑两次（或者对比一个**金标准基线**），断言两份 SRT 完全一致。

没有真模型时不算环境故障 —— 它直接报告 skip，因为组件级确定性已由
tests/test_pipeline.py 钉住，这里是在完整链路里再验一次。

用法：
    VIDSUB_LLAMA_SERVER=<llama-server> python tools/check_regression.py <视频> [--baseline <基线srt>]

判定条件：权重齐备（本机 D:/vsdata 已备好），否则按 skip 处理。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from vidsub import downloader, pipeline, runtime  # noqa: E402


def _ready() -> bool:
    models = runtime.default_model_root()
    if not os.path.isdir(models):
        return False
    try:
        return all(downloader.status_of(s).state == "ready"
                   for s in downloader.all_specs(models))
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--baseline")
    ap.add_argument("--out", default="regression")
    args = ap.parse_args()

    if not os.path.isfile(args.video):
        print(f"视频不存在：{args.video}")
        return 2
    if not _ready():
        print("SKIP：真模型权重不齐备，无法做完整链路回归（组件级确定性已由")
        print("      tests/test_pipeline.py 钉住）。")
        return 0

    os.environ.setdefault("VIDSUB_IDLE_TIMEOUT", "0")
    rt = runtime.Runtime(model_root=runtime.default_model_root(),
                         idle_timeout=0.0)
    try:
        rt.ensure_started(runtime.default_services())
    except runtime.StartupError as e:
        print(f"推理服务起不来：{e}")
        return 2

    os.makedirs(args.out, exist_ok=True)
    a, b = (os.path.join(args.out, f"{i}.srt") for i in (1, 2))
    try:
        pipeline.run(args.video, a)
        pipeline.run(args.video, b)
    finally:
        rt.stop_all()

    if open(a, "rb").read() == open(b, "rb").read():
        print("OK：对同一输入跑两次，字节级一致。")
        code = 0
    else:
        print("FAIL：两次字幕不一致，确定性受损。")
        code = 1

    if args.baseline:
        if open(a, "rb").read() == open(args.baseline, "rb").read():
            print("OK：与基线一致。")
        else:
            print("FAIL：与基线不一致。")
            code = 1
    return code


if __name__ == "__main__":
    sys.exit(main())