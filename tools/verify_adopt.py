"""拿真实的 r2t2-test 权重走一遍收编，确认不下载也能就位。

不是正式测试，是端到端手工验证：
    python tools/verify_adopt.py [源目录] [数据目录]

这个脚本以前自己拼「扫目录 → spec_for → adopt → 逐条报告 → 查最终状态」，
和 server.py::rescan_caches、verify_runtime.py 各写了一遍。现在这套协议由
`models.ModelStore.acquire()` 提供 —— 脚本只剩下打印。

第 3 步的"逐条报告"正是当初要求 `acquire()` 必须返回逐条 outcome 的原因：
诊断脚本要能看到每个权重是硬链接还是复制、成功还是失败。
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from vidsub import models                      # noqa: E402

DEFAULT_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", "r2t2-test")


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SRC
    root = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.environ.get("TEMP", "."), "vidsub_adopt", "models")

    print(f"源目录 : {os.path.abspath(src)}")
    print(f"数据目录: {os.path.abspath(root)}")
    print()

    store = models.ModelStore(root)

    print("=== 1. 标准缓存探测（应该多为空）===")
    before = store.inspect()
    for i in before.items:
        print(f"  {i.key:10} {i.size_text:>10}  {i.source or '未找到'}")
    print()

    print("=== 2. 收编指定目录（校验 sha256 → 硬链接 / 复制）===")
    t0 = time.time()
    rep = store.acquire(os.path.abspath(src))
    print(f"  认出并处理 {sum(1 for o in rep.outcomes if o.found)} 个权重，"
          f"耗时 {time.time() - t0:.1f}s")
    for o in rep.outcomes:
        if not o.found:
            print(f"    {o.key:10} 未找到")
            continue
        how = "hardlink" if o.linked else ("skip" if o.skipped else "copy")
        state = "OK" if (o.ok or o.skipped) else "FAIL " + o.error
        print(f"    {o.key:10} {o.size_text:>10}  {how:8} {state}")
    print()

    print("=== 3. 最终状态 ===")
    for i in rep.report.items:
        print(f"  {i.key:10} {i.state:8} {i.size_text:>10}  {i.abs_path}")
    print()
    print("全部就绪，无需下载。" if rep.report.ready else "还有权重没就绪。")
    return 0 if rep.report.ready else 1


if __name__ == "__main__":
    sys.exit(main())
