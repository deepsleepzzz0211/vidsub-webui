"""拿真 llama-server + 真权重跑一遍运行时（03 号票的端到端验证）。

证明的不是"替身能跑"，而是"参数、路径、生命周期在真二进制上成立"。
真二进制会对含空格的模型路径报 `invalid argument`，替身学得再像也
不代表真机没问题 —— 所以必须真跑一次。

    python tools/verify_runtime.py [权重源目录] [数据目录] [llama-server 路径]

注意数据目录要**不含空格**且与源目录**同卷**，认领才能走硬链接（零拷贝）。
本机项目目录 "workbuddy en" 带空格，正好是这条约束的现实用例。
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from vidsub import models, runtime                       # noqa: E402

DEFAULT_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "r2t2-test")


def human(n: int) -> str:
    return f"{n / 1024 ** 2:.1f} MB"


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SRC
    root = sys.argv[2] if len(sys.argv) > 2 else r"D:\vsdata\models"
    exe = sys.argv[3] if len(sys.argv) > 3 else None
    exe = exe or os.path.join(os.path.abspath(src), "bin",
                              runtime.binary_name())

    print(f"权重源  : {os.path.abspath(src)}")
    print(f"数据目录: {root}")
    print(f"llama-server: {exe}")
    print()

    print("=== 0. 前置检查 ===")
    if not os.path.isfile(exe):
        print(f"  ✗ 找不到 {exe}")
        return 1
    print(f"  ✓ llama-server 存在（{human(os.path.getsize(exe))}）")
    if " " in root:
        print(f"  ✗ 数据目录含空格：{root}")
        print("     含空格的模型路径会让 llama-server 报 invalid argument")
        return 1
    print("  ✓ 数据目录不含空格")

    print()
    print("=== 1. 认领权重（校验 sha256）===")
    t0 = time.time()
    rep = models.ModelStore(root).acquire(os.path.abspath(src))
    for o in rep.outcomes:
        if not o.found:
            print(f"  ✗ 源目录里没有 {o.key}")
            return 1
        how = "hardlink" if o.linked else ("skip" if o.skipped else "copy")
        print(f"  {o.key:10} {o.size_text:>10}  {how:8} "
              f"{'OK' if (o.ok or o.skipped) else 'FAIL ' + o.error}")
        if not (o.ok or o.skipped):
            return 1
    print(f"  耗时 {time.time() - t0:.1f}s")

    print()
    print("=== 2. 启动两个推理服务（真二进制，串行）===")
    os.environ["VIDSUB_LLAMA_SERVER"] = exe
    rt = runtime.Runtime(model_root=root, idle_timeout=0.0,
                         startup_timeout=240.0)
    t0 = time.time()
    try:
        services = rt.ensure_started(runtime.default_services())
        print(f"  两个服务就绪，耗时 {time.time() - t0:.1f}s")
        for s in services:
            print(f"  {s['key']:4} :{s['port']}  pid {s['pid']}  "
                  f"{s['state']}  {s['model']}")
    except runtime.StartupError as e:
        print(f"  ✗ {e}")
        # 部分失败时已起来的进程不能留着 —— 否则真二进制白占着内存
        rt.stop_all()
        return 1

    try:
        print()
        print("=== 3. 复用（第二个作业不该重新加载）===")
        t0 = time.time()
        again = rt.ensure_started(runtime.default_services())
        same = all(a["pid"] == b["pid"] for a, b in zip(services, again))
        print(f"  复用耗时 {time.time() - t0:.2f}s，pid 未变：{same}")
        if not same:
            print("  ✗ 服务被重启了 —— 没达到常驻复用的目的")
            return 1

        print()
        print("=== 4. 日志确实落盘 ===")
        for s in services:
            size = os.path.getsize(s["log_path"])
            tail_ = runtime.tail(s["log_path"], n=2)
            print(f"  {s['key']:4} {human(size):>10}  {s['log_path']}")
            for line in tail_.splitlines():
                print(f"       {line[:110]}")

        print()
        print("=== 5. 退出清理（不留残余进程）===")
        pids = [s["pid"] for s in services]
        rt.stop_all()
        for pid in pids:
            deadline = time.time() + 20
            while time.time() < deadline and runtime.alive(pid):
                time.sleep(0.2)
            state = "仍在" if runtime.alive(pid) else "已回收"
            flag = "✗" if runtime.alive(pid) else "✓"
            print(f"  {flag} pid {pid} {state}")

        left = [p for p in pids if runtime.alive(p)]
        print()
        if left:
            print(f"有 {len(left)} 个进程残留。")
            return 1
        print("全部回收干净，内存已归还。")
    finally:
        # 兜底：不论上面怎么走，都要收进程。
        # 部分失败时已起来的进程不能留着 —— 否则真二进制白占着内存。
        rt.stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())