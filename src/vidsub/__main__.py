"""入口：python -m vidsub

启动顺序有讲究，一步都不能错：

1. 先看是不是已经有实例在跑 —— 有就别再起一个（两份模型会互相拖慢），
   直接在浏览器里打开它就退出。
2. 挑一个空闲端口。
3. **先让 server 真的 listening，再打开浏览器**。反过来用户看到的是
   ERR_CONNECTION_REFUSED。
4. 记下端口，让下次启动能认出自己。
"""
import os
import sys
import webbrowser

import uvicorn

from . import launcher
from .server import app, install_signal_handlers


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    host = "127.0.0.1"          # 只绑本机，不对局域网暴露
    fixed_port = None
    if "--port" in argv:
        try:
            fixed_port = int(argv[argv.index("--port") + 1])
        except (IndexError, ValueError):
            print("--port 需要一个端口号", file=sys.stderr)
            return 2

    # 1. 已有实例？打开它，别再起一个
    existing = (fixed_port if fixed_port is not None
                else launcher.find_existing_instance())
    if existing is not None and launcher.is_our_instance(existing):
        url = f"http://127.0.0.1:{existing}/"
        print(f"vidsub 已在运行：{url}")
        print("如需重启，请先关闭那个窗口。")
        if not os.environ.get("VIDSUB_NO_BROWSER"):
            try:
                webbrowser.open(url)
            except Exception:
                pass    # 打不开浏览器不算错误，URL 已打印
        return 0

    # 2. 端口：指定了就用指定的并检查冲突，否则自动挑空闲
    if fixed_port is not None:
        port = fixed_port
        if launcher.is_our_instance(port):
            print(f"端口 {port} 已被 vidsub 占用，请换一个或直接访问 "
                  f"http://127.0.0.1:{port}/", file=sys.stderr)
            return 1
    else:
        port = launcher.pick_port()

    install_signal_handlers()
    launcher.write_record(port)

    print(f"vidsub 启动中 …  http://127.0.0.1:{port}/")
    print("按 Ctrl+C 停止")

    # 3. 浏览器在后台等 server 就绪后打开，不阻塞主流程
    launcher.open_browser_after_ready(port)

    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        launcher.clear_record()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
