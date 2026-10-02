"""按域名选路的真实验证：拿真实的 ModelScope 与 GitHub 各下一次。

重点验证两件事：
- ModelScope 走**直连**（35 MB/s），不被代理拖成 240 KB/s
- GitHub（VAD）走**代理**，拿到完整文件而不是 268KB 就断

    python tools/verify_proxy.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from vidsub import downloader, registry          # noqa: E402


def probe(url: str, want_bytes: int, label: str) -> None:
    proxied = downloader._use_proxy(url)
    host = downloader._host_of(url)
    t0 = time.time()
    try:
        with downloader._open(url) as r:
            got = r.read(want_bytes)
        dt = time.time() - t0
        print(f"  {label}")
        print(f"    域名 {host}  路线 {'代理' if proxied else '直连'}")
        print(f"    取到 {len(got)} 字节，耗时 {dt:.1f}s，"
              f"{len(got) / 1024 / max(dt, 0.01):.0f} KB/s")
        if len(got) < want_bytes:
            print(f"    ⚠ 只拿到 {len(got)}/{want_bytes} 字节 —— 被截断了")
        else:
            print("    ✓ 完整")
    except Exception as e:                       # noqa: BLE001
        print(f"  {label}")
        print(f"    域名 {host}  路线 {'代理' if proxied else '直连'}")
        print(f"    ✗ {type(e).__name__}: {e}")


def main() -> None:
    print("代理配置:",
          os.environ.get(downloader.PROXY_ENV) or "(未设置，走直连)")
    print()

    ms = registry.get("asr_model").modelscope_url()
    vad = registry.get("vad").direct_url
    probe(ms, 24 * 1024 * 1024, "ModelScope 识别主模型（期望直连且快）")
    print()
    probe(vad, registry.get("vad").size_bytes, "GitHub Silero VAD（期望走代理且完整）")
    print()
    print("已直连失败的域名（之后会自动走代理）:",
          sorted(downloader._direct_failed) or "无")


if __name__ == "__main__":
    main()