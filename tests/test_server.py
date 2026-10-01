"""服务的可测行为：探测端点、首页、退出清理

这些是启动器依赖的契约：探测端点必须存在，否则启动器认不出自己；
子进程必须能被登记并在退出时收掉。
"""
import subprocess
import sys
import time
import urllib.request

import pytest

from vidsub import launcher

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(url: str, timeout: float = 5.0):
    with _OPENER.open(url, timeout=timeout) as r:
        return r.status, r.read()


@pytest.fixture
def live_server():
    """起一个真实的 uvicorn 子进程，返回 (port, proc)"""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "vidsub.server:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=__file__.rsplit("\\", 1)[0] + r"\..",
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # 等它真的能服务
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            _get(f"http://127.0.0.1:{port}/api/health", timeout=1)
            break
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError("uvicorn 启动失败")
            time.sleep(0.2)
    try:
        yield port, proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_probe_endpoint_answers(live_server):
    """启动器靠这个端点认出自己的实例"""
    port, _ = live_server
    status, _body = _get(f"http://127.0.0.1:{port}{launcher.PROBE_PATH}")
    assert status == 200


def test_index_page_served(live_server):
    port, _ = live_server
    status, body = _get(f"http://127.0.0.1:{port}/")
    assert status == 200
    assert b"vidsub" in body


def test_launcher_detects_live_vidsub_instance(live_server):
    """真实服务起来后，启动器必须能认出它"""
    port, _ = live_server
    assert launcher.is_our_instance(port) is True
