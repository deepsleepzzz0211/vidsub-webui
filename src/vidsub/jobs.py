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
        self._running: set = set()      # 防止同一 job 被并发 start 两次启两条线程
        os.makedirs(self.root, exist_ok=True)

    # --- 查询 ------------------------------------------------------------

    def get(self, job_id: str) -> Optional[dict]:
        path = os.path.join(self.root, job_id, "job.json")
        if not os.path.isfile(path):
            return None
        try:
            return json.load(open(path, encoding="utf-8"))
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
        jdir = os.path.join(self.root, job["id"])
        os.makedirs(jdir, exist_ok=True)
        tmp = os.path.join(jdir, "job.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False, indent=2)
        os.replace(tmp, os.path.join(jdir, "job.json"))

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
        """后台跑流水线。幂等：已经在跑就不重复启动。"""
        job = self.get(job_id)
        if not job:
            raise JobError(f"job 不存在：{job_id}")
        with self._lock:
            if job_id in self._running:
                return job           # 已经在跑，别再启一条线程
            self._running.add(job_id)
            state = job["state"]

        if state == RUNNING:
            with self._lock:
                self._running.discard(job_id)
            return job
        self.update(job_id, state=RUNNING, progress={"i": 0, "total": 0,
                                                      "label": "排队中"})
        t = threading.Thread(target=self._run, args=(job_id,), daemon=True,
                             name=f"vidsub-job-{job_id}")
        t.start()
        return self.get(job_id)

    def _run(self, job_id: str) -> None:
        job = self.get(job_id)
        if not job:
            return
        self.update(job_id, state=RUNNING, progress={"i": 0, "total": 0,
                                                      "label": "解析视频"})
        try:
            def _progress(i: int, total: int):
                j = self.get(job_id)
                if j:
                    self.update(job_id, progress={"i": i, "total": total,
                                                   "label": f"识别中 {i}/{total}"})

            pipeline.run(
                job["video"], job["srt"],
                work_dir=job["work_dir"],
                progress=_progress,
            )
            self.update(job_id, state=DONE,
                        progress={"i": 1, "total": 1, "label": "完成"})
        except (audio.MediaError, vad.NoSpeechError,
                pipeline.PipelineError) as e:
            self.update(job_id, state=FAILED, error=str(e),
                        progress={"i": 0, "total": 0, "label": "失败"})
        except Exception as e:  # noqa: BLE001
            self.update(job_id, state=FAILED, error=f"{type(e).__name__}: {e}",
                        progress={"i": 0, "total": 0, "label": "失败"})
        finally:
            with self._lock:
                self._running.discard(job_id)

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