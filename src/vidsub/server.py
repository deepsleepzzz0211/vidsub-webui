"""Web 服务

本票只做骨架：一个能起来的首页 + 供启动器探测存活的端点。
上传、字幕、压制在后续工单里补。

关于退出清理：Windows 上以 detached 方式起的子进程不会随父进程退出而消失，
会残留成孤儿、白占几 GB 内存。所以所有子进程都要登记到这里，退出时统一收掉。
"""
import atexit
import os
import signal
import subprocess
import sys
import threading

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from . import launcher

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")

app = FastAPI(title="vidsub", docs_url=None, redoc_url=None)

# 已登记的子进程 pid，退出时统一 terminate
_CHILDREN: list[int] = []
_CHILDREN_LOCK = threading.Lock()


def register_child(pid: int) -> None:
    """登记一个需要在退出时清理的子进程"""
    with _CHILDREN_LOCK:
        _CHILDREN.append(pid)


def _terminate_children() -> None:
    """收掉所有登记过的子进程。

    注意 capture_output=True 已经接管了 stdout/stderr，再传 stdout= 会直接
    抛 ValueError 而**静默被 except 吞掉** —— 清理形同虚设，进程照样残留。
    """
    with _CHILDREN_LOCK:
        pids, _CHILDREN[:] = list(_CHILDREN), []
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=15)
        except Exception:
            # 单个 pid 失败（已退出等）不能影响后面的
            pass


atexit.register(_terminate_children)


def _on_signal(signum, _frame):
    """Ctrl+C / 关窗口时先收子进程，再退出"""
    _terminate_children()
    launcher.clear_record()
    sys.exit(128 + signum)


def install_signal_handlers() -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            # 非主线程注册会失败，忽略即可
            pass


@app.get(launcher.PROBE_PATH)
def alive():
    """启动器用它判断"是不是已经有实例在跑"，刻意用不太可能撞车的路径"""
    return JSONResponse({"status": "ok"})


@app.get("/")
def index():
    page = os.path.join(WEB_DIR, "index.html")
    if os.path.exists(page):
        return FileResponse(page)
    return JSONResponse({"message": "vidsub 骨架已就绪"})


@app.get("/api/health")
def health():
    return {"status": "ok"}
