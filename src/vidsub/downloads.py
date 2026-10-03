"""下载任务管理：串行队列 + 进度广播

为什么必须串行：CPU 与磁盘带宽都是独占资源，并发下载只会互相拖慢，
而且进度条会乱跳。对 2.9GB 的总量来说，串行的总时长更短。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import downloader

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_ERROR = "error"


@dataclass
class Task:
    id: str
    state: str = STATE_IDLE
    error: str = ""
    items: dict = field(default_factory=dict)      # key -> {state, have, total, label}
    canceled: bool = False

    def as_dict(self) -> dict:
        total = sum(i["total"] for i in self.items.values())
        have = sum(i["have"] for i in self.items.values())
        return {
            "id": self.id,
            "state": self.state,
            "error": self.error,
            "progress": round(have / total, 4) if total else 0.0,
            "have_bytes": have,
            "total_bytes": total,
            "items": [
                {
                    "key": i["key"], "label": i["label"],
                    "state": i["state"], "have": i["have"],
                    "total": i["total"], "note": i.get("note", ""),
                }
                for i in self.items.values()
            ],
        }


class DownloadManager:
    """单 worker 队列。状态在内存里，进程退出即失效（合理：权重还在磁盘上）。"""

    def __init__(self, root: str, on_change: Optional[Callable] = None):
        self.root = root
        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}
        self._current: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._on_change = on_change

    # --- 状态 ---

    def snapshot(self, task_id: str = None) -> dict:
        with self._lock:
            tid = task_id or self._current or self._latest_id()
            if tid is None:
                return self._all_ready_or_needed()
            t = self._tasks.get(tid)
            return t.as_dict() if t else self._all_ready_or_needed()

    def _latest_id(self) -> Optional[str]:
        return next(reversed(self._tasks), None) if self._tasks else None

    def _all_ready_or_needed(self) -> dict:
        """没有任务时的初始视图：直接反映磁盘现状"""
        specs = downloader.all_specs(self.root)
        items, total, have = [], 0, 0
        all_ready = True
        for s in specs:
            st = downloader.status_of(s)
            items.append({"key": s.key, "label": s.label, "state": st.state,
                          "have": st.have_bytes, "total": st.total_bytes,
                          "note": s.license_note})
            total += st.total_bytes
            have += (st.total_bytes if st.state == "ready" else st.have_bytes)
            if st.state != "ready":
                all_ready = False
        return {"id": None, "state": STATE_IDLE, "error": "",
                "progress": 1.0 if all_ready and total else
                             (round(have / total, 4) if total else 0.0),
                "have_bytes": have, "total_bytes": total, "items": items}

    def active(self) -> Optional[str]:
        """有任务在跑就返回它的 id，否则 None。

        与 `snapshot()` 的区别：**这个不碰磁盘**。`snapshot()` 在没有任务的
        时候会逐个查权重状态，而查状态要对整份权重算 sha256 —— 实测 2.4 GB
        要 2.17 秒。首页每次加载都会问"有没有在下的"，用 snapshot() 就等于
        每次开页面把 2.4 GB 从磁盘读一遍。

        凡是不需要逐项进度、只想知道"有没有任务"的地方，一律用这个。
        """
        with self._lock:
            for tid, t in self._tasks.items():
                if t.state in (STATE_IDLE, STATE_RUNNING):
                    return tid
            return None

    def all_ready(self) -> bool:
        return all(downloader.status_of(s).state == "ready"
                   for s in downloader.all_specs(self.root))

    def missing(self) -> list[dict]:
        return [{"key": s.key, "label": s.label,
                 "state": downloader.status_of(s).state}
                for s in downloader.all_specs(self.root)
                if downloader.status_of(s).state != "ready"]

    # --- 队列 ---

    def start(self) -> str:
        tid = uuid.uuid4().hex[:12]
        with self._lock:
            self._tasks[tid] = Task(id=tid)
            if self._thread and self._thread.is_alive():
                return tid
            self._thread = threading.Thread(
                target=self._worker, daemon=True,
                name=f"download-{tid}")
            self._thread.start()
        return tid

    def _worker(self):
        while True:
            with self._lock:
                tid = next((i for i in self._tasks
                            if self._tasks[i].state == STATE_IDLE), None)
                if tid is None:
                    self._current = None
                    return
                self._current = tid
                task = self._tasks[tid]
                task.state = STATE_RUNNING

            try:
                ok = self._run_task(task)
                with self._lock:
                    # 只在成功时标 done。_run_task 失败时它自己已置 error，
                    # 这里若无脑写 done 会把失败**覆盖**掉 —— 页面上会显示
                    # "完成"，但其实有文件没下好。
                    if ok and task.state != STATE_ERROR:
                        task.state = STATE_DONE
            except Exception as e:                # 兜底，别让 worker 静默死掉
                with self._lock:
                    task.state = STATE_ERROR
                    task.error = f"{type(e).__name__}: {e}"
            finally:
                self._notify()

    def _run_task(self, task: Task) -> bool:
        """跑完一个任务。返回是否全部成功。"""
        specs = downloader.all_specs(self.root)
        with self._lock:
            task.items = {
                s.key: {"key": s.key, "label": s.label, "state": "pending",
                       "have": 0, "total": s.size_bytes, "note": s.license_note}
                for s in specs
            }

        for spec in specs:
            with self._lock:
                if task.canceled:
                    return False
                st = downloader.status_of(spec)
                task.items[spec.key].update(
                    state=st.state, have=st.have_bytes, total=st.total_bytes)
            self._notify()

            def on_progress(done: int, total: int, _k=spec.key):
                with self._lock:
                    if _k in task.items:
                        task.items[_k].update(have=done, total=total, state="downloading")
                self._notify()

            res = downloader.download_one(spec, progress=on_progress)
            with self._lock:
                task.items[spec.key]["state"] = ("ready" if res.ok
                                                 else "failed")
                if not res.ok:
                    task.items[spec.key]["note"] = res.error
            self._notify()
            if not res.ok:
                with self._lock:
                    task.state = STATE_ERROR
                    task.error = f"{spec.label} 下载失败：{res.error}"
                return False

        # 协议全文随权重一起落盘（网易协议 3.4b 要求每个副本保留）
        downloader.copy_license_files(self.root)
        with self._lock:
            for item in task.items.values():
                if item["state"] != "ready":
                    item["state"] = "ready"
        return True

    def _notify(self):
        if self._on_change:
            try:
                self._on_change()
            except Exception:
                pass

    def wait_idle(self, timeout: float = 600.0) -> bool:
        """等到没有任务在跑。

        判定要覆盖「已排队但还没开始」的情况：worker 刚被唤醒、
        还没把状态置为 running 时，会有一瞬间所有任务都是 idle。
        早期版本只查 running，于是那一瞬间就误判为"已空闲"并提前返回，
        紧接着任务才开始跑 —— 调用方以为结束了，其实还在下。
        """
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._lock:
                pending = any(t.state in (STATE_IDLE, STATE_RUNNING)
                              for t in self._tasks.values())
                if not pending and self._current is None:
                    return True
            time.sleep(0.1)
        return False
