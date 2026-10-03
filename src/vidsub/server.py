"""Web 服务

本票只做骨架：一个能起来的首页 + 供启动器探测存活的端点。
上传、字幕、压制在后续工单里补。

关于退出清理：Windows 上以 detached 方式起的子进程不会随父进程退出而消失，
会残留成孤儿、白占几 GB 内存。所以所有子进程都要登记到这里，退出时统一收掉。
"""
import atexit
import json
import os
import signal
import subprocess
import sys
import threading

from fastapi import FastAPI, UploadFile, File
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)

from . import audio, launcher, models, runtime
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
    if not store().inspect().ready:
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


_store: "models.ModelStore | None" = None
_store_root: str = ""
_store_lock = threading.Lock()
_jobs: "JobManager | None" = None


def jobs() -> JobManager:
    global _jobs
    if _jobs is None:
        from .jobs import _jobs_root
        _jobs = JobManager(_jobs_root())
    return _jobs


def store() -> models.ModelStore:
    """取全局唯一的模型资产接缝。

    按数据目录缓存：目录变了就重建。早先只判断"有没有实例"，一旦进程内
    改过 `VIDSUB_DATA_DIR`（测试隔离、嵌入式调用），就会拿着旧目录继续跑，
    表现为"权重写到了奇怪的地方"或"明明下载完了却说没就绪"。
    """
    global _store, _store_root
    root = data_dir()
    with _store_lock:
        if _store is None or _store_root != root:
            _store = models.ModelStore(root)
            _store_root = root
        return _store


@app.post("/api/models/rescan")
def rescan_caches():
    """去 HuggingFace / ModelScope 的标准缓存里找已有权重，找到就收编。

    收编而非重下：用户早就下过的模型不该再拉一遍 2.6 GB。
    收编前逐个过 sha256，认错了宁可不动。
    """
    rep = store().acquire()
    return {**rep.as_dict(), **models_status()}


@app.get("/api/models")
def models_status():
    """下载页要的一切：逐权重现状 + 找过哪些缓存 + 缺失指引 + 下载进度。

    一个 `inspect()` 拿全。这几样本散在四个模块里（清单 / 扫缓存 / 下载 /
    后台任务），拼装逻辑在这个文件里重写过两遍、在 tools/ 里重写过两遍 ——
    现在调用方只需要知道这一个方法。
    """
    return store().inspect().as_dict()


@app.post("/api/models/download")
def start_download():
    """开始（或排队）下载。重复调用是安全的：已就绪的会被跳过。"""
    task = store().download()
    return {"task_id": task.id, "state": "running"}


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

    try:
        rep = store().acquire(path)
    except models.SourceError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not rep.any_found:
        return JSONResponse(
            {"error": "该目录里没找到可用的权重（按 sha256 校验）",
             "scanned": path}, status_code=404)
    return {**rep.as_dict(), **models_status()}


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
    """取全局唯一的运行时。按模型目录缓存（同 store 的理由）。

    用独立的 Runtime 锁而不是复用 _store_lock：stop_all 要花秒级去杀
    进程，拿着资产接缝那把锁做这事会让所有状态查询一起卡住。
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
    missing = list(store().inspect().missing_keys)
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
    if not store().inspect().ready:
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


# SSE 的存活上限。**不能设成"作业预计时间的量级"** —— 05 号票的前提就是
# 70 分钟视频要跑 20 分钟以上，600 秒（10 分钟）会在作业跑到一半时把连接
# 掐断，页面从此不再更新，用户以为死了。给足 12 小时：正常作业远到不了，
# 而真到上限时也该由浏览器重连兜底，不是让服务端替用户放弃。
SSE_MAX_TICKS = int(os.environ.get("VIDSUB_SSE_MAX_SECONDS", "43200"))


def job_event_stream(job_id: str, max_ticks: int = 0, pause: float = 1.0,
                     heartbeat_every: int = 15):
    """作业进度的 SSE 事件流。每次状态变化推一条 data:。

    **烧录也算作业的一部分**：done 只代表字幕写完，压制还在跑的时候流必须
    留着，否则页面永远看不到"压制中 → 完成"。收尾条件是"作业到终态 **且**
    没有压制在进行"。

    `heartbeat_every` 用来打注释帧（`: keepalive`）：状态长时间不变的阶段
    （一段长推理可能几十秒）不发数据的话，反向代理会把连接当空闲掐掉。

    收尾条件刻意**不含**「等用户点压制」：那意味着一个做完字幕的作业会
    占住一个 anyio 线程池线程直到上限（几小时），几十个历史作业就能把
    线程池抽干。压制进度由前端在点压制时**重新订阅**一次流来跟 ——
    每次订阅都是有界的，这比让服务端替用户空等要稳。
    """
    import time as _t
    if max_ticks <= 0:
        max_ticks = SSE_MAX_TICKS
    last = None
    for tick in range(max_ticks):
        j = jobs().get(job_id)
        if not j:
            yield "event: error\ndata: 作业不存在\n\n"
            return
        info = {k: j.get(k) for k in ("id", "state", "error", "progress", "burn_state")}
        payload = json.dumps(info, ensure_ascii=False)
        if payload != last:
            yield f"data: {payload}\n\n"
            last = payload
        elif heartbeat_every and tick % heartbeat_every == 0:
            yield ": keepalive\n\n"
        if j.get("state") in ("done", "failed") and j.get("burn_state") != "running":
            return
        _t.sleep(pause)


@app.get("/api/jobs/{job_id}/stream")
def job_stream(job_id: str):
    return StreamingResponse(job_event_stream(job_id),
                             media_type="text/event-stream")


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
