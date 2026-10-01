"""E2E 服务夹具：在隔离的临时目录里起一个 vidsub 实例

核心约束：**绝不读写开发者自己的 ~/.vidsub**。测试要能安全地在日常使用的
机器上跑，否则没人愿意跑 E2E。

隔离手段：
- 设置 VIDSUB_DATA_DIR 指向临时目录（启动器据此决定记录文件位置）
- 随机空闲端口，避免与开发者本机在跑���实例冲突
- 只绑定 127.0.0.1
"""
import contextlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = "/__vidsub_alive"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def is_up(port: int, timeout: float = 1.0) -> bool:
    try:
        with _OPENER.open(f"http://127.0.0.1:{port}{PROBE}", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


@contextlib.contextmanager
def running_server(port: int | None = None, data_dir: str | None = None,
                   timeout: float = 40.0):
    """起一个隔离的 vidsub 实例，退出时确保收干净。"""
    port = port or free_port()
    owned_tmp = data_dir is None
    tmp = data_dir or tempfile.mkdtemp(prefix="vidsub-e2e-")
    os.makedirs(tmp, exist_ok=True)

    env = dict(os.environ)
    env["VIDSUB_DATA_DIR"] = tmp          # 隔离：不碰用户自己的 ~/.vidsub
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env.pop(k, None)                   # 免得本地请求被代理劫持
    env["VIDSUB_NO_BROWSER"] = "1"         # 别在测试机上弹浏览器窗口

    proc = subprocess.Popen(
        [sys.executable, "-m", "vidsub", "--port", str(port)],
        cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _stop():
        # Windows 上 terminate() 是 TerminateProcess：服务拿不到 atexit，
        # 它登记的推理子进程不会被回收，会残留成孤儿（白占几 GB）。
        # 必须按 pid 连带子树一起杀 —— 与 src/vidsub/server.py 同一手法。
        if proc.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=20)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=20)
            else:
                proc.kill()
            proc.wait(timeout=10)

    deadline = time.time() + timeout
    ready = False
    while time.time() < deadline:
        if is_up(port, timeout=0.5):
            ready = True
            break
        if proc.poll() is not None:
            err = (proc.stderr.read() or b"").decode("utf-8", "replace")[-800:]
            raise RuntimeError(
                f"服务启动即退出（码 {proc.returncode}）。\n"
                f"  stderr: {err or '(空)'}\n"
                f"  常见原因：fastapi/uvicorn 未安装，或端口被占。\n"
                f"  数据目录 {tmp}")
        time.sleep(0.2)
    if not ready:
        _stop()
        raise RuntimeError(f"服务在 {timeout} 秒内没就绪（端口 {port}）")

    try:
        yield {"port": port, "data_dir": tmp, "pid": proc.pid,
               "base_url": f"http://127.0.0.1:{port}"}
    finally:
        _stop()
        if owned_tmp:
            shutil.rmtree(tmp, ignore_errors=True)
