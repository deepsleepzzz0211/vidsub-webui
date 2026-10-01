"""拿真实的 r2t2-test 权重走一遍收编，确认不下载也能就位。

不是正式测试，是端到端手工验证：
    python tools/verify_adopt.py [源目录] [数据目录]
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from vidsub import discovery, downloader, registry          # noqa: E402

DEFAULT_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", "r2t2-test")


def main() -> None:
    src = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SRC
    root = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.environ.get("TEMP", "."), "vidsub_adopt", "models")

    print(f"源目录 : {os.path.abspath(src)}")
    print(f"数据目录: {os.path.abspath(root)}")
    print()
    print("=== 1. 标准缓存探测（应该多为空）===")
    got = discovery.find_all()
    for a in registry.ASSETS:
        hit = got.get(a.key)
        print(f"  {a.key:10} {discovery.human_size(a.size_bytes):>10}  "
              f"{hit['source'] if hit else '未找到'}")
    print()

    print("=== 2. 按 sha256 扫描用户指定目录 ===")
    t0 = time.time()
    hits = downloader.scan_directory(src)
    print(f"  认出 {len(hits)} 个权重，扫描耗时 {time.time() - t0:.1f}s")
    for h in hits:
        print(f"    {h.key:10} {os.path.basename(h.path)}")
    print()

    print("=== 3. 收编（校验 → 硬链接）===")
    t0 = time.time()
    for h in hits:
        spec = downloader.spec_for(registry.get(h.key), root)
        r = downloader.adopt(spec, h.path)
        how = "hardlink" if r.linked else "copy"
        state = "OK" if r.ok else "FAIL " + r.error
        print(f"  {h.key:10} {discovery.human_size(spec.size_bytes):>10}  "
              f"{how:8} {state}")
    print(f"  耗时 {time.time() - t0:.1f}s")
    print()

    print("=== 4. 最终状态 ===")
    ok = True
    for s in downloader.all_specs(root):
        st = downloader.status_of(s)
        ok = ok and st.state == "ready"
        size = (discovery.human_size(os.path.getsize(s.dest))
                if os.path.exists(s.dest) else "-")
        print(f"  {s.key:10} {st.state:8} {size:>10}  {s.dest}")
    print()
    print("全部就绪，无需下载。" if ok else "还有权重没就绪。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())