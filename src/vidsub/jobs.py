"""作业管理：上传一个视频 → 后台跑流水线 → 产物持久化

每个作业一个目录：`<数据目录>/jobs/<job_id>/` —— 视频原件、工作目录
（抽音轨中间品）、`out.srt`（产物）、`job.json`（状态与进度）。

目录放在模型目录**同级**，由 default_model_root 保证不含空格 —— 否则
临时音轨/中间 wav 一路传给 ffmpeg 时路径里带空格就出幺蛾子（实测
ffmpeg 的 `subtitles=` 滤镜和 llama-server 一样怕空格）。

状态机：pending → running → (done | failed)
进度：`progress` dict `{i, total, label}`，页面轮询它。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Optional

from . import audio, pipeline, vad
from . import burn as burn_mod

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

# job.json 是热点文件：SSE 每秒读、页面每 1.2 秒读、worker 每段写。
# Windows 上 os.replace 撞上别人的读句柄会 PermissionError，重试即可。
_PERSIST_RETRIES = 8


class JobError(RuntimeError):
    def __init__(self, error: str):
        super().__init__(error)
        self.error = error


def _jobs_root() -> str:
    from . import runtime
    root = runtime.default_model_root()
    return os.path.join(os.path.dirname(root.rstrip("/\\")), "jobs")


def _now() -> float:
    return time.time()


class JobManager:
    def __init__(self, root: Optional[str] = None):
        self.root = root or _jobs_root()
        self._lock = threading.RLock()
        self._running: set = set()      # 防止同一 job 被并发 start 两次
        self._queue: list = []             # 串行队列（作业一个个来，CPU 推理是独占的）
        self._worker: Optional[threading.Thread] = None
        self._burn_lock = threading.Lock()  # 压制也排队，不许两个 ffmpeg 抢 CPU
        os.makedirs(self.root, exist_ok=True)

    # --- 查询 ------------------------------------------------------------

    def get(self, job_id: str) -> Optional[dict]:
        path = os.path.join(self.root, job_id, "job.json")
        if not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def list(self) -> list:
        out = []
        for d in os.listdir(self.root):
            j = self.get(d)
            if j:
                out.append(j)
        out.sort(key=lambda j: j.get("created_at", 0), reverse=True)
        return out

    def _persist(self, job: dict) -> None:
        """原子落盘：先写 .tmp 再 os.replace，读者永远看不到半个 JSON。

        Windows 上 os.replace 会因为**别的线程正开着 job.json 读**而报
        PermissionError（默认不共享 delete 权限）。job.json 是热点文件 ——
        SSE 每秒读一次、页面每 1.2 秒轮询一次、worker 每段写一次，三方
        撞上就失败。所以要重试，而不是把读者全锁起来（那会把轮询堵死）。
        """
        jdir = os.path.join(self.root, job["id"])
        os.makedirs(jdir, exist_ok=True)
        tmp = os.path.join(jdir, "job.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False, indent=2)
        dest = os.path.join(jdir, "job.json")
        for attempt in range(_PERSIST_RETRIES):
            try:
                os.replace(tmp, dest)
                return
            except PermissionError:
                if attempt == _PERSIST_RETRIES - 1:
                    raise
                time.sleep(0.02 * (attempt + 1))

    # --- 创建 ------------------------------------------------------------

    def create(self, filename: str, size_bytes: int, info: dict) -> dict:
        """新建一个 job，并把上传的视频落盘到持久目录。"""
        job_id = uuid.uuid4().hex[:12]
        jdir = os.path.join(self.root, job_id)
        os.makedirs(jdir, exist_ok=True)
        ext = os.path.splitext(filename)[1]
        video = os.path.join(jdir, "input" + (ext if ext else ".mp4"))
        job = {
            "id": job_id,
            "state": PENDING,
            "filename": filename,
            "created_at": _now(),
            "updated_at": _now(),
            "error": "",
            "progress": {"i": 0, "total": 0, "label": "等待开始"},
            "info": info,
            "video": video,
            "srt": os.path.join(jdir, "out.srt"),
            "work_dir": os.path.join(jdir, "work"),
            "burn_state": "",
            "video_mp4": "",
        }
        self._persist(job)
        return job

    def update(self, job_id: str, **fields) -> dict:
        with self._lock:
            job = self.get(job_id)
            if not job:
                raise JobError(f"job 不存在：{job_id}")
            job.update(fields)
            job["updated_at"] = _now()
            self._persist(job)
            return job

    # --- 执行 ------------------------------------------------------------

    def start(self, job_id: str) -> dict:
        """入队跑流水线。幂等：已在队列/在跑/已完成的不重复排。"""
        job = self.get(job_id)
        if not job:
            raise JobError(f"job 不存在：{job_id}")
        with self._lock:
            if job_id in self._queue or job_id in self._running:
                return job
            if job["state"] == DONE:
                return job
            self._queue.append(job_id)
            self.update(job_id, state=PENDING,
                        progress={"i": 0, "total": 0, "label": "排队中，等轮到执行"})
            self._ensure_worker()
        return self.get(job_id)

    def _ensure_worker(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._worker_loop,
                                        daemon=True, name="vidsub-jobs-worker")
        self._worker.start()

    def _worker_loop(self) -> None:
        """单 worker 串行执行：CPU 推理独占，同一时刻只能跑一个 job。"""
        while True:
            with self._lock:
                if not self._queue:
                    self._worker = None
                    return
                job_id = self._queue.pop(0)
                self._running.add(job_id)
            try:
                self._run(job_id)
            finally:
                with self._lock:
                    self._running.discard(job_id)

    def _run(self, job_id: str) -> None:
        """真正跑一个 job（只在 worker 里被调用）。

        全程持有 runtime 引用计数：作业跑多久（09 号票是 20 分钟量级）
        都不会被空闲巡检回收模型 —— 不用靠"每段打一次 touch"的侥幸。
        """
        job = self.get(job_id)
        if not job:
            return
        self.update(job_id, state=RUNNING, progress={"i": 0, "total": 0,
                                                      "label": "解析视频"})
        # 延迟导入：server 顶层 import 了 jobs，顶层再 import 回去就成环。
        # 单例 rt() 住在 server 里（它和 manager 共用同一套目录切换逻辑）。
        from . import server as server_mod
        rt = server_mod.rt()
        rt.acquire()
        try:
            pipeline.run(
                job["video"], job["srt"],
                work_dir=job["work_dir"],
                progress=self._progress_cb(job_id),
                on_stage=lambda label: self.update(
                    job_id, progress={"i": 0, "total": 0, "label": label}),
            )
            self.update(job_id, state=DONE,
                        progress={"i": 1, "total": 1, "label": "完成"})
            # 中间品删掉，产物（out.srt / subtitled.mp4 / input）留着。
            # 12 号票要求"素材用完即清理"：70 分钟视频会切出几百个
            # seg_*.wav，加上整条 audio.wav，能吃掉几个 GB。08 号票要求
            # "产物不自动删" —— 两者不冲突，因为删的只是中间品。
            self._clean_work(job_id)
        except (audio.MediaError, vad.NoSpeechError,
                pipeline.PipelineError) as e:
            self.update(job_id, state=FAILED, error=str(e),
                        progress={"i": 0, "total": 0, "label": "失败"})
        except Exception as e:  # noqa: BLE001
            self.update(job_id, state=FAILED, error=f"{type(e).__name__}: {e}",
                        progress={"i": 0, "total": 0, "label": "失败"})
        finally:
            rt.release()

    # 暂存目录里的中间品文件名。audio.wav/seg_* 来自流水线，in.*/sub*.srt
    # 来自压制（burn 会把源和字幕复制进去再跑 ffmpeg，跑完就是两份几百 MB
    # 的垃圾）。产物 out.srt / subtitled.mp4 / input.* 都不在列。
    _WORK_JUNK = ("audio.wav", "seg_", "in.", "sub.srt", "sub_zh.srt")

    def _clean_work(self, job_id: str) -> int:
        """删掉中间品（音频切片、压制暂存），保留产物。返回删掉的文件数。

        失败时静默跳过：清理是锦上添花，不该让一个已经成功的作业变成失败。
        作业失败时**不删** —— 中间品是排查"为什么这段识别不出来"的现场。
        """
        job = self.get(job_id)
        if not job:
            return 0
        work = job.get("work_dir") or ""
        if not work or not os.path.isdir(work):
            return 0
        n = 0
        try:
            for name in os.listdir(work):
                if any(name == p or name.startswith(p)
                       for p in self._WORK_JUNK):
                    try:
                        os.remove(os.path.join(work, name))
                        n += 1
                    except OSError:
                        pass
        except OSError:
            pass
        return n

    def _progress_cb(self, job_id: str):
        """段落进度：写状态 + 顺手 touch 一次（引用计数之外的第二道保险）。"""
        def _cb(i: int, total: int, stage: str = "", translated: int = 0):
            if self.get(job_id):
                label = stage or f"识别中 {i}/{total}"
                self.update(job_id, progress={"i": i, "total": total,
                                              "label": label,
                                              "translated": translated})
                try:
                    from . import server as server_mod
                    server_mod.rt().touch()
                except Exception:      # noqa: BLE001
                    pass               # 状态已经落盘，touch 失败不该弄坏作业
        return _cb

    # --- 压制 ---

    def start_burn(self, job_id: str, mono: bool = False) -> dict:
        """字幕完成后喊一声「压制成片」。幂等：已经有压制在跑就不重启。"""
        job = self.get(job_id)
        if not job:
            raise JobError(f"job 不存在：{job_id}")
        if job["state"] != DONE:
            raise JobError("字幕还没生成，不能压")
        if job.get("burn_state") == "running":
            return job
        self.update(job_id, burn_state="running", error="")
        t = threading.Thread(target=self._burn, args=(job_id, mono), daemon=True,
                             name=f"vidsub-burn-{job_id}")
        t.start()
        return self.get(job_id)

    def _burn(self, job_id: str, mono: bool) -> None:
        # 压制也串行：ffmpeg 是 CPU 密集型，两个并行压只会互相拖慢
        with self._burn_lock:
            job = self.get(job_id)
            if not job:
                return
            try:
                out = os.path.join(os.path.dirname(job["srt"]), "subtitled.mp4")
                burn_mod.burn(job["video"], job["srt"], out, mono=mono,
                              stage_dir=job["work_dir"])
                self.update(job_id, burn_state="done", video_mp4=out)
            except burn_mod.BurnError as e:
                self.update(job_id, burn_state="failed", error=str(e))
            except Exception as e:  # noqa: BLE001
                self.update(job_id, burn_state="failed",
                            error=f"{type(e).__name__}: {e}")
            finally:
                # burn 会把源视频和字幕复制进暂存目录再跑 ffmpeg，跑完就是
                # 两份几百 MB 的垃圾。同样只清中间品，成片留着。
                self._clean_work(job_id)