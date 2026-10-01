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
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import discovery, downloader, launcher
from . import registry
from .downloads import DownloadManager

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
    # 首次运行缺权重时，直接把人送到下载页 —— 否则会看到"能上传"的界面，
    # 然后在真正需要模型的时刻才失败。
    if not manager().all_ready():
        return RedirectResponse("/downloads", status_code=307)
    page = os.path.join(WEB_DIR, "index.html")
    if os.path.exists(page):
        return FileResponse(page)
    return JSONResponse({"message": "vidsub 骨架已就绪"})


@app.get("/api/health")
def health():
    return {"status": "ok"}


# --- 模型下载 -----------------------------------------------------------
#
# 权重落在一个**不含空格**的目录里：含空格的绝对路径会让 llama-server
# 拒绝启动、让 ffmpeg 的字幕滤镜打不开文件。这条约束贯穿全项目。

def data_dir() -> str:
    base = os.environ.get("VIDSUB_DATA_DIR") or os.path.join(
        os.path.expanduser("~"), ".vidsub")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "models")


_manager: DownloadManager | None = None
_manager_root: str = ""
_manager_lock = threading.Lock()


def manager() -> DownloadManager:
    """取全局唯一的下载管理器。

    按数据目录缓存：目录变了就重建。早先只判断"有没有实例"，一旦进程内
    改过 `VIDSUB_DATA_DIR`（测试隔离、嵌入式调用），就会拿着旧目录继续跑，
    表现为"权重写到了奇怪的地方"或"明明下载完了却说没就绪"。
    """
    global _manager, _manager_root
    root = data_dir()
    with _manager_lock:
        if _manager is None or _manager_root != root:
            _manager = DownloadManager(root)
            _manager_root = root
        return _manager


@app.post("/api/models/rescan")
def rescan_caches():
    """去 HuggingFace / ModelScope 的标准缓存里找已有权重，找到就收编。

    收编而非重下：用户早就下过的模型不该再拉一遍 2.6 GB。
    收编前逐个过 sha256，认错了宁可不动。
    """
    root = data_dir()
    found = discovery.find_all()
    results = []
    for asset in registry.ASSETS:
        hit = found.get(asset.key)
        if not hit:
            results.append({"key": asset.key, "ok": False,
                            "error": "标准缓存里没找到"})
            continue
        res = downloader.adopt(downloader.spec_for(asset, root), hit["path"])
        results.append({"key": asset.key, "label": asset.label,
                        "source": hit["source"], "source_path": hit["path"],
                        "ok": res.ok, "linked": res.linked,
                        "skipped": res.skipped, "error": res.error})
    downloader.copy_license_files(root)
    return {"results": results, **models_status()}


def cache_report() -> dict:
    """下载页要展示的"我们找过哪些地方"—— 让用户知道不是随便没找到"""
    return {
        "roots": [{"kind": r.kind, "path": r.path, "exists": r.exists,
                   "env_var": r.env_var} for r in discovery.cache_roots()],
        "found": discovery.find_all(),
    }


@app.get("/api/models")
def models_status():
    """权重现状：列表 + 体积 + 进度。前端据此决定显示上传页还是下载页。"""
    m = manager()
    snap = m.snapshot()
    missing_keys = [i["key"] for i in snap["items"] if i["state"] != "ready"]
    return {
        "ready": m.all_ready(),
        "missing": m.missing(),
        "caches": cache_report(),
        # 找不到时给"去魔搭下载"的指引（单文件、可续传）
        "hints": discovery.hints_for_missing(missing_keys),
        **snap,
    }


@app.post("/api/models/download")
def start_download():
    """开始（或排队）下载。重复调用是安全的：已就绪的会被跳过。"""
    task_id = manager().start()
    return {"task_id": task_id, "state": "running"}


@app.get("/api/models/status")
def download_status(task_id: str | None = None):
    return manager().snapshot(task_id)


@app.post("/api/models/adopt")
def adopt_manual(payload: dict = None):
    """用户手动指一个目录：按 sha256 认领里面已有的权重。

    只认用户**明确指出**的目录，不主动扫 home —— 自动探测一律走
    标准缓存目录（见 discovery）。
    """
    path = (payload or {}).get("path", "").strip()
    if not path:
        return JSONResponse({"error": "缺少 path"}, status_code=400)
    if not os.path.isdir(path):
        return JSONResponse({"error": f"目录不存在：{path}"}, status_code=400)

    root = data_dir()
    hits = downloader.scan_directory(path)
    results = []
    by_key = {h.key: h for h in hits}
    for asset in registry.ASSETS:
        hit = by_key.get(asset.key)
        if not hit:
            continue
        res = downloader.adopt(downloader.spec_for(asset, root), hit.path)
        results.append({"key": asset.key, "label": asset.label,
                        "path": hit.path, "ok": res.ok,
                        "linked": res.linked, "error": res.error})
    if not results:
        return JSONResponse(
            {"error": "该目录里没找到可用的权重（按 sha256 校验）",
             "scanned": path}, status_code=404)
    downloader.copy_license_files(root)
    return {"results": results, **models_status()}


@app.get("/downloads")
def downloads_page():
    page = os.path.join(WEB_DIR, "downloads.html")
    if os.path.exists(page):
        return FileResponse(page)
    return JSONResponse({"message": "下载页未就绪"})
