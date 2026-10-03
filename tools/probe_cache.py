"""看一眼标准缓存里到底有什么，以及找不到时长什么样。

不是正式测试，是探针脚本：
    python tools/probe_cache.py

这个脚本以前自己拼「扫缓存 → 逐权重对下落」的协议（和 server.py、
verify_adopt.py 各写了一遍）。现在这些都由 `models.ModelStore` 提供 ——
脚本只剩下打印。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from vidsub import models, runtime          # noqa: E402


def main() -> int:
    rep = models.ModelStore(runtime.default_model_root()).inspect()

    print(f"模型目录: {rep.root}")
    print()
    print("=== 认出的缓存根 ===")
    for r in rep.caches:
        env = r.env_var or "(默认位置)"
        print(f"  {r.kind:12} exists={r.exists!s:5} 由 {env} 决定")
        print(f"    -> {r.path}")

    print()
    print("=== 四个权重在标准缓存里的下落 ===")
    for i in rep.items:
        where = (f"命中（来自 {i.source}）" if i.source
                 else ("已就位" if i.state == models.STATE_READY else "未找到"))
        print(f"  {i.key:10} {i.size_text:>10}  {where}")
        if i.source:
            print(f"             {i.path}")

    if rep.hints:
        print()
        print(f"=== 缺 {len(rep.hints)} 个，给出去魔搭下载的指引 ===")
        for h in rep.hints:
            print(f"  [{h['label']}] {h['size_text']}")
            print(f"    仓库 {h['repo']}  文件 {h['path']}")
            print(f"    落盘 {h['target']}")
            print(f"    {h['curl']}")
            print()
        print("提示：", rep.hints[0]["note"])
    else:
        print()
        print("四个权重都齐了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
