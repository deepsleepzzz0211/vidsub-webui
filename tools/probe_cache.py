"""看一眼标准缓存里到底有什么，以及找不到时长什么样。

不是正式测试，是探针脚本：
    python tools/probe_cache.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from vidsub import discovery, registry          # noqa: E402


def main() -> None:
    print("=== 认出的缓存根 ===")
    for r in discovery.cache_roots():
        env = r.env_var or "(默认位置)"
        print(f"  {r.kind:12} exists={r.exists!s:5} 由 {env} 决定")
        print(f"    -> {r.path}")

    print()
    print("=== 四个权重在标准缓存里的下落 ===")
    got = discovery.find_all()
    for a in registry.ASSETS:
        hit = got.get(a.key)
        where = f"命中（来自 {hit['source']}）" if hit else "未找到"
        print(f"  {a.key:10} {a.size_bytes / 1024 ** 2:8.1f} MB  {where}")
        if hit:
            print(f"             {hit['path']}")

    missing = [a.key for a in registry.ASSETS if a.key not in got]
    if missing:
        print()
        print(f"=== 缺 {len(missing)} 个，给出去魔搭下载的指引 ===")
        for h in discovery.hints_for_missing(missing):
            print(f"  [{h['label']}] {h['size_text']}")
            print(f"    仓库 {h['repo']}  文件 {h['path']}")
            print(f"    落盘 {h['target']}")
            print(f"    {h['curl']}")
            print()
        print("提示：", discovery.hints_for_missing(missing)[0]["note"])


if __name__ == "__main__":
    main()