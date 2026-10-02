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

from fastapi import FastAPI, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import audio, discovery, downloader, launcher
from . import registry, runtime
from .downloads import DownloadManager
from .jobs import JobManager

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

    推理服务由 runtime 自己管（它知道自己的 pid），这里只兜底收其它遗留。
    """
    try:
        if _rt is not None:
            _rt.stop_all()
    except Exception:
        pass
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
    """权重落盘目录。

    刻意**委托**给 runtime.default_model_root()，不自己再拼一遍：那个函数
    知道"用户名带空格时要躲开"这条约束（llama-server 收到含空格的模型绝对
    路径会报 invalid argument 直接拒绝启动）。早先两边各拼一份，结果服务侧
    完全绕开了躲避逻辑 —— 对 "John Smith" 这样的用户名，每次启动都撞
    SpaceInPathError，而躲避逻辑一次都没执行过。
    """
    root = runtime.default_model_root()
    os.makedirs(root, exist_ok=True)
    return root


_manager: DownloadManager | None = None
_manager_root: str = ""
_manager_lock = threading.Lock()
_jobs: "JobManager | None" = None


def jobs() -> JobManager:
    global _jobs
    if _jobs is None:
        from .jobs import _jobs_root
        _jobs = JobManager(_jobs_root())
    return _jobs


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


# --- 推理运行时 ---------------------------------------------------------
#
# 识别与翻译两个 llama-server 常驻复用，第一个作业结束后不退出。
# 缺权重时不要贸然启动 —— 那必然失败，先让用户去下载页。

def rt() -> runtime.Runtime:
    """取全局唯一的运行时。按模型目录缓存（同 manager 的理由）。

    用独立的 Runtime 锁而不是复用 _manager_lock：stop_all 要花秒级去杀
    进程，拿着下载管理器那把锁做这事会让所有下载状态查询一起卡住。
    """
    global _rt, _rt_root
    root = data_dir()
    with _rt_lock:
        if _rt is None or _rt_root != root:
            old = _rt
            _rt = runtime.Runtime(
                model_root=root,
                idle_timeout=float(os.environ.get(runtime.IDLE_ENV, "1800")),
            )
            _rt_root = root
            if old is not None:
                old.stop_all()
        return _rt


_rt: "runtime.Runtime | None" = None
_rt_root = ""
_rt_lock = threading.Lock()


@app.get("/api/runtime")
def runtime_status():
    """推理服务现状：状态、端口、pid、日志位置"""
    return {"services": rt().status()}


@app.post("/api/runtime/start")
def runtime_start():
    """把两个推理服务拉起来。重复调用安全：已在跑的直接复用。"""
    m = manager()
    missing = [i["key"] for i in m.missing()]
    if missing:
        return JSONResponse(
            {"error": "权重还没齐，先去下载页", "missing": missing},
            status_code=409)
    try:
        services = rt().ensure_started(runtime.default_services())
    except runtime.StartupError as e:
        # 消息里已经带了日志尾巴与具体是哪个服务，直接透传给用户
        return JSONResponse({"error": str(e)}, status_code=503)
    return {"services": services}


@app.post("/api/runtime/stop")
def runtime_stop():
    """手动关掉推理服务（释放内存）"""
    rt().stop_all()
    return {"services": rt().status()}


@app.post("/api/runtime/touch")
def runtime_touch():
    """作业在跑，标记一下以推迟空闲退出"""
    rt().touch()
    return {"ok": True}


@app.get("/api/runtime/log")
def runtime_log(key: str = "", tail: int = 60):
    """查看某个推理服务的日志尾巴。

    没有它，日志落盘就形同虚设：起不来时错误里带一截，真起来之后出
    问题却没地方看。key 传服务名；不传默认看 asr。
    """
    key = key or "asr"
    services = {s["key"]: s for s in rt().status()}
    if key not in services:
        return JSONResponse({"error": f"未知服务 {key}"}, status_code=404)
    log_path = services[key]["log_path"]
    if not os.path.isfile(log_path):
        return JSONResponse({"key": key, "log": "（日志文件还不存在）"})
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return JSONResponse({"error": f"读不到日志：{e}"}, status_code=500)
    return {"key": key, "log_path": log_path,
            "log": "".join(lines[-max(tail, 1):])}


# --- 上传处理与作业 -------------------------------------------------------

@app.post("/api/jobs")
async def create_job(file: UploadFile = File(...)):
    """上传视频 → 探测它 → 建 job → 后台跑流水线。"""
    m = manager()
    if not m.all_ready():
        return JSONResponse({"error": "模型还没齐全，先去下载页"}, status_code=409)
    if not file.filename:
        return JSONResponse({"error": "没有选择文件"}, status_code=400)
    job = jobs().create(file.filename, file.size or 0, {})
    try:
        data = await file.read()
    except Exception:
        return JSONResponse({"error": "读取上传的文件失败"}, status_code=500)
    with open(job["video"], "wb") as f:
        f.write(data)
    try:
        info = audio.probe_media(job["video"])
    except audio.MediaError as e:
        jobs().update(job["id"], state="failed", error=str(e))
        return JSONResponse({"error": str(e)}, status_code=422)
    jobs().update(job["id"], info={
        "duration": info.duration, "width": info.width, "height": info.height,
        "size_bytes": info.size_bytes, "has_audio": info.has_audio,
    })
    if not info.has_audio:
        jobs().update(job["id"], state="failed",
                      error="这个文件没有音频轨，无法识别语音")
        return JSONResponse({"error": "文件没有音频轨"}, status_code=422)
    # 模型权重要已就绪，后台才能跑通
    import threading as _t
    _t.Thread(target=_ensure_runtime_then_start, args=(job["id"],), daemon=True).start()
    return jobs().get(job["id"])


def _ensure_runtime_then_start(job_id: str) -> None:
    try:
        rt().ensure_started(runtime.default_services())
    except runtime.StartupError as e:
        jobs().update(job_id, state="failed", error=str(e))
        return
    jobs().start(job_id)


@app.get("/api/jobs")
def list_jobs():
    return {"jobs": jobs().list()}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    j = jobs().get(job_id)
    if not j:
        return JSONResponse({"error": "job 不存在"}, status_code=404)
    return j


@app.get("/api/jobs/{job_id}/srt")
def get_srt(job_id: str):
    j = jobs().get(job_id)
    if not j:
        return JSONResponse({"error": "job 不存在"}, status_code=404)
    if j["state"] != "done":
        return JSONResponse({"error": "字幕还没做完", "state": j["state"]},
                            status_code=409)
    return FileResponse(j["srt"], media_type="text/plain; charset=utf-8",
                        filename="subtitles.srt")


@app.post("/api/jobs/{job_id}/burn")
def burn_job(job_id: str, mono: bool = False):
    """字幕完成后压制成片。后台跑，状态走 job['burn_state']。"""
    try:
        return jobs().start_burn(job_id, mono=mono)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=409)


@app.get("/api/jobs/{job_id}/video")
def get_video(job_id: str):
    j = jobs().get(job_id)
    if not j:
        return JSONResponse({"error": "job 不存在"}, status_code=404)
    if j.get("burn_state") != "done" or not j.get("video_mp4"):
        return JSONResponse({"error": "成片还没准备好", "burn_state": j.get("burn_state")},
                            status_code=409)
    return FileResponse(j["video_mp4"], media_type="video/mp4",
                        filename="subtitled.mp4")
