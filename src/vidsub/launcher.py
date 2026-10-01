"""启动器：端口选择、已有实例探测、浏览器打开时机

三件事各自都踩过坑，所以拆成窄的、可测的函数：

1. **端口**：固定端口被占用就起不来。要自动挑空闲的。
2. **已有实例**：重复执行启动器不能再起一个 server——两个实例会各占一份
   几 GB 内存互相拖慢。探测到就在浏览器里打开它，然后干净退出。
3. **浏览器时机**：必须在 server 真的 listening 之后才 open。提前 open
   会让用户看到 ERR_CONNECTION_REFUSED，这是这类工具最常见的差评来源。
"""
import json
import os
import socket
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

# 探测用路径。带上一个不太可能撞车的路径名，避免把别的本地服务误判成我们。
PROBE_PATH = "/__vidsub_alive"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def pick_port(avoid: int | None = None) -> int:
    """挑一个空闲端口，绑 0 让系统分配，然后关掉让 uvicorn 重新绑。

    关掉和重新绑之间有个极小的竞态窗口，对本地单用户工具可接受。
    """
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port != avoid:
            return port
    raise RuntimeError("找不到空闲端口")


def _probe_url(port: int) -> str:
    return f"http://127.0.0.1:{port}{PROBE_PATH}"


def is_our_instance(port: int, timeout: float = 1.0) -> bool:
    """该端口上是否已经有一个 vidsub 实例在跑"""
    try:
        with _OPENER.open(_probe_url(port), timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def wait_until_serving(port: int, timeout: float = 30.0,
                       interval: float = 0.1) -> bool:
    """轮询直到端口可服务。返回是否在超时前就绪。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_our_instance(port, timeout=0.5):
            return True
        time.sleep(interval)
    return False


def open_browser_after_ready(port: int, ready=None, timeout: float = 30.0):
    """server 就绪后在新线程里打开浏览器，不阻塞主线程。

    ready 是一个可选回调，在探测之前调用（用于让探测服务器真正开始 serve，
    测试里需要）。生产路径里 uvicorn 已经在另一个线程跑了，不需要它。
    """
    if ready is not None:
        ready()

    def _wait_and_open():
        if wait_until_serving(port, timeout=timeout):
            webbrowser.open(f"http://127.0.0.1:{port}/")

    t = threading.Thread(target=_wait_and_open, daemon=True)
    t.start()
    return t


def find_running_instance(candidates) -> int | None:
    """在候选端口里找出已经在跑的实例"""
    for port in candidates:
        if is_our_instance(port):
            return port
    return None


class _ProbeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == PROBE_PATH:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):   # 别往 stderr 刷日志
        pass


def make_probe_server(port: int) -> HTTPServer:
    """测试用：起一个只会回应探测请求的极简 server"""
    return HTTPServer(("127.0.0.1", port), _ProbeHandler)


def find_existing_instance(record_path: str | None = None) -> int | None:
    """读上次运行时记录的端口并探测。

    比盲扫端口可靠：只认自己写下的那个。
    """
    record_path = record_path or default_record_path()
    try:
        with open(record_path, "r", encoding="utf-8") as f:
            port = int(json.load(f)["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return port if is_our_instance(port) else None


def default_record_path() -> str:
    base = os.environ.get("VIDSUB_DATA_DIR") or os.path.join(
        os.path.expanduser("~"), ".vidsub")
    return os.path.join(base, "instance.json")


def write_record(port: int, record_path: str | None = None) -> None:
    record_path = record_path or default_record_path()
    os.makedirs(os.path.dirname(record_path), exist_ok=True)
    with open(record_path, "w", encoding="utf-8") as f:
        json.dump({"port": port}, f)


def clear_record(record_path: str | None = None) -> None:
    record_path = record_path or default_record_path()
    try:
        os.remove(record_path)
    except OSError:
        pass
