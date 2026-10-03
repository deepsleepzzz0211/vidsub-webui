"""模型资产：清单 × 落盘根 × 传输，收成一个接缝。

**为什么要有这个模块。** 清单、扫本机缓存、下载、后台任务这四件事本来散在
四个模块里（`registry` / `discovery` / `downloader` / `downloads`），它们各自
都对，但**协议漏到了调用方**：

- 「查现状」（`find_all()` → 逐 asset 拼 key/label/size/state/source/path）
  被写了 **4 遍**：`server.py` 两处、`tools/probe_cache.py`、`tools/verify_adopt.py`
- 「收编」（scan → `spec_for` → `adopt` → `copy_license_files`）
  被写了 **3 遍**：`server.py::rescan_caches`、`tools/verify_adopt.py`、
  `tools/verify_runtime.py`

后果是 `server.py` 为了提供这些能力直接 import 了 8 个模块，每个路由在现场
拼多模块序列。它的路由是浅的 —— 删掉它，复杂度会原样出现在下一个调用方。

**接口只有三个入口点**（深模块：小接口 + 厚实现）：

    inspect()   一次拿全：就绪 / 逐权重现状 / 缓存根 / 缺失指引 / 路径
    acquire()   收编：source=None 走标准缓存，给了路径则走用户目录
    download()  后台下载任务句柄

底层四个模块降为**内部接缝**：接口不变，既有测试继续有效，将来想真正内联
实现也可以不改这个接缝。

**`root` 必须由调用方传入**（`runtime.default_model_root()`）。本模块只校验、
绝不自己拼目录 —— 「用户名含空格要躲开」的逻辑全项目只能有一份。历史上
复制过一次，结果服务侧完全绕开了躲避逻辑：对 "John Smith" 这样的用户名
每次启动都撞 `SpaceInPathError`，而躲避逻辑一次都没执行过。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from . import discovery, downloader, registry, runtime
from .downloads import DownloadManager

# 权重状态的取值域。与 downloader.Status.state 一致，这里复述一遍是为了让
# 调用方不必 import downloader —— 那个模块属于本模块的内部。
STATE_MISSING = "missing"
STATE_PARTIAL = "partial"
STATE_READY = "ready"
STATE_CORRUPT = "corrupt"


class SourceError(ValueError):
    """收编源不合法（目录不存在等）。接口层转 400。

    归到 ValueError 下面：`ValueError` 是 Python 里"参数不对"的通用说法，
    调用方用 `except ValueError` 就能一次抓全，不用记住本模块的类名。
    """


@dataclass(frozen=True)
class AssetView:
    """一个权重的现状。**快照**，不是活视图 —— 要新状态重新 inspect()。"""
    key: str
    label: str
    state: str
    size_bytes: int
    size_text: str
    have_bytes: int
    source: str          # 命中的缓存来源；已就位或没找到时为空
    path: str            # 命中的缓存路径 / 落盘路径
    rel_path: str        # 相对 root，喂 llama-server 用（分隔符恒为 /）
    abs_path: str
    purpose: str = ""
    license_note: str = ""

    def as_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label, "state": self.state,
            "size_bytes": self.size_bytes, "size_text": self.size_text,
            "have_bytes": self.have_bytes, "source": self.source,
            "path": self.path, "rel_path": self.rel_path,
            "abs_path": self.abs_path, "purpose": self.purpose,
            "license_note": self.license_note,
        }


@dataclass(frozen=True)
class CacheRootView:
    kind: str
    path: str
    exists: bool
    env_var: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "path": self.path,
                "exists": self.exists, "env_var": self.env_var}


@dataclass(frozen=True)
class Report:
    """`inspect()` 的返回值：下载页要的一切，一次拿全。

    刻意做成一个聚合对象而不是五个方法 —— 调用方（下载页、诊断脚本）本来
    就是同时需要这几样，拆开只会让每个调用点都写一遍同样的拼装。这个拼装
    以前在 `server.py` 里写过两遍、`tools/` 里写过两遍。

    `download` 是下载任务的快照（没任务时为空 dict）。带上它是因为下载页
    要同时显示"磁盘上有什么"和"有没有在下的"，而这两件事本来就是两回事。
    """
    root: str
    items: tuple
    caches: tuple
    found: dict
    hints: tuple
    verified: bool
    download: dict = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return all(i.state == STATE_READY for i in self.items)

    @property
    def missing_keys(self) -> tuple:
        return tuple(i.key for i in self.items if i.state != STATE_READY)

    @property
    def total_bytes(self) -> int:
        return sum(i.size_bytes for i in self.items)

    @property
    def have_bytes(self) -> int:
        return sum(i.have_bytes for i in self.items)

    @property
    def progress(self) -> float:
        total = self.total_bytes
        return round(self.have_bytes / total, 4) if total else 0.0

    def as_dict(self) -> dict:
        """下载页的载荷。**字段名是页面契约**，改了页面会静默坏掉。

        `items` 在有下载任务时取自任务快照：磁盘上看不出 `downloading`
        这个状态，只有任务知道。没任务时才从磁盘现状合成 —— 两者是同一批
        权重的两种视图，页面对它们的读法一致。
        """
        task = dict(self.download or {})
        task_items = task.pop("items", None)
        if task_items:
            items = task_items
        else:
            items = [{"key": i.key, "label": i.label, "state": i.state,
                      "have": i.have_bytes, "total": i.size_bytes,
                      "note": i.license_note} for i in self.items]
        # 没有任务时也要给出 `state`（idle）与 `error`（空）：页面无条件读
        # 这两个字段，缺了会拿到 undefined。契约稳定比少一个键重要。
        task.setdefault("state", "idle")
        task.setdefault("error", "")
        return {
            "ready": self.ready,
            "items": items,
            "missing": [i.as_dict() for i in self.items
                        if i.state != STATE_READY],
            "caches": {"roots": [c.as_dict() for c in self.caches],
                       "found": self.found},
            "hints": list(self.hints),
            "total_bytes": self.total_bytes,
            "have_bytes": self.have_bytes,
            "progress": self.progress,
            "verified": self.verified,
            # 任务级字段（state / error / id / progress）由它带上来
            **task,
        }


@dataclass(frozen=True)
class AdoptOutcome:
    """一个权重的收编结果。逐条报告是调用方的硬需求（诊断脚本要打印）。"""
    key: str
    label: str
    size_bytes: int
    size_text: str
    found: bool          # 源里有没有这个权重
    ok: bool             # 收编成功
    linked: bool = False     # 硬链接（零额外空间）还是复制
    skipped: bool = False    # 目标已就绪，跳过
    source: str = ""
    source_path: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label, "found": self.found,
            "ok": self.ok, "linked": self.linked, "skipped": self.skipped,
            "size_bytes": self.size_bytes, "size_text": self.size_text,
            "source": self.source, "source_path": self.source_path,
            "error": self.error,
        }


@dataclass(frozen=True)
class AcquireReport:
    """收编结果 + 收编后的现状。

    带上 `report` 是为了省掉调用方紧跟着的一次 `inspect()` —— 收编过程里
    本来就要逐个查状态，再查一遍等于把同一件事做两次。
    """
    source: str
    outcomes: tuple
    report: Report

    @property
    def any_found(self) -> bool:
        return any(o.found for o in self.outcomes)

    def as_dict(self) -> dict:
        return {"source": self.source,
                "results": [o.as_dict() for o in self.outcomes]}


@dataclass
class Task:
    """后台下载任务的句柄。

    进度只能经它读，不碰 `DownloadManager` 的线程内部 —— 那是内部接缝。
    """
    id: str
    _manager: DownloadManager = field(repr=False)

    def progress(self) -> dict:
        return self._manager.snapshot(self.id)

    def wait(self, timeout: float = 600.0) -> bool:
        return self._manager.wait_idle(timeout)


class ModelStore:
    """模型资产的唯一接缝。

    构造一次、反复用。线程安全性与底层一致：`inspect()` 只读可并发，
    `acquire()` / `download()` 会写盘，调用方应串行。
    """

    def __init__(self, root: str):
        if not root:
            raise ValueError("root 不能为空；请传 runtime.default_model_root()")
        self.root = root
        # 只校验、不计算。含空格的路径会让 llama-server 拒绝启动，
        # 与其等到起服务时才报，不如在构造时就挡住。
        if " " in os.path.abspath(root):
            raise runtime.SpaceInPathError(
                f"模型目录不能含空格：{root}\n"
                f"  llama-server 收到含空格的模型路径会报 invalid argument。\n"
                f"  用 VIDSUB_DATA_DIR 换一个不含空格的目录。")
        self._manager = DownloadManager(root)
        # key → (体积, mtime)：校验通过的证据。文件被换掉时 mtime 会变，
        # 缓存自然失效，不需要手动清。
        self._verified: dict = {}

    # --- 入口点 1：现状 -------------------------------------------------

    def inspect(self, verify: bool = False) -> Report:
        """当前现状的快照。只读、幂等。

        **`verify=False`（默认）走廉价路径**：体积对得上就判 ready，不重算
        哈希。这不是偷懒 —— 全量校验 2.4 GB 的权重实测要 **2.17 秒**，而
        首页分流、建作业闸口、下载页轮询都会调它。每次都算，等于每次开页面
        都要把 2.4 GB 从磁盘读一遍。

        一旦某次真的校验通过，就把 (路径, 体积, mtime) 记下来，之后连体积
        比对都省了。

        `verify=True` 强制全量校验，用在「怀疑磁盘有问题」或用户主动点
        「检查完整性」时。代价就是那 2.17 秒。

        代价说清楚：**不校验时，一份体积正好对得上但内容已损坏的权重会被
        判成 ready**。这个风险是可接受的 —— 真损坏时 llama-server 加载会
        明确失败，`StartupError` 会把日志尾巴带出来，用户看到的是"服务起
        不来 + 原因"，而不是静默算错。
        """
        found = discovery.find_all()
        items = []
        for asset in registry.ASSETS:
            spec = downloader.spec_for(asset, self.root)
            state, have = self._state_of(spec, verify)
            hit = found.get(asset.key)
            # 路径优先给"实际可用的那一份"：已就位就给落盘位置，
            # 否则给缓存里的命中位置（下载页据此提示"可以收编"）。
            path = spec.dest if state == STATE_READY else (
                hit["path"] if hit else spec.dest)
            items.append(AssetView(
                key=asset.key, label=asset.label, state=state,
                size_bytes=spec.size_bytes,
                size_text=discovery.human_size(spec.size_bytes),
                have_bytes=have,
                source=(hit["source"] if hit and state != STATE_READY else ""),
                path=path,
                rel_path=registry.rel_path(asset),
                abs_path=spec.dest,
                purpose=asset.purpose, license_note=asset.license_note))

        report = Report(
            root=self.root, items=tuple(items),
            caches=tuple(CacheRootView(kind=c.kind, path=c.path,
                                       exists=c.exists, env_var=c.env_var)
                         for c in discovery.cache_roots()),
            found=found,
            hints=tuple(discovery.hints_for_missing(
                [i.key for i in items if i.state != STATE_READY])),
            verified=verify,
            # 下载页要同时显示"磁盘上有什么"和"有没有在下的"，所以把任务
            # 快照一并带上 —— 调用方不必再自己去问一遍下载管理器。
            #
            # ⚠️ 只在**真有任务**时才取快照。`snapshot()` 在无任务时会去逐个
            # 查权重状态（含全量哈希，2.4 GB / 2.17 秒），而首页每次加载都会
            # 调 inspect()。这一条如果写成无条件调用，整个模块的性能目标就
            # 白定了 —— 实测踩过一次。
            download=(self._manager.snapshot(tid)
                      if (tid := self._manager.active()) else {}))
        return report

    def _state_of(self, spec, verify: bool):
        """便宜地判断一个权重的状态。返回 `(state, have_bytes)`。

        分支顺序是刻意的：
        1. 校验过且 (体积, mtime) 没变 → 直接 ready，一次 stat 就够
        2. verify=True → 走 downloader.status_of（全量哈希）
        3. 廉价路径下体积正好等于期望 → 认作 ready，并记进已验证集
        4. 其余（缺失 / 半成品 / 体积不对）→ 走 downloader.status_of

        第 3 条是**唯一新增的判断**，也是这个模块最需要测试钉住的地方：
        它把"信任磁盘"从"每次重算"里摘出来。第 4 条兜住真正的异常形态
        （体积不足、超出、边车不匹配），那些情况 status_of 本来就不算哈希。
        """
        try:
            size = os.path.getsize(spec.dest)
            mtime = os.path.getmtime(spec.dest)
        except OSError:
            self._verified.pop(spec.key, None)
            st = downloader.status_of(spec)
            return st.state, st.have_bytes

        if self._verified.get(spec.key) == (size, mtime):
            return STATE_READY, size

        if not verify and size == spec.size_bytes and size > 0:
            self._verified[spec.key] = (size, mtime)
            return STATE_READY, size

        st = downloader.status_of(spec)
        if st.state == STATE_READY:
            self._verified[spec.key] = (size, mtime)
        else:
            self._verified.pop(spec.key, None)
        return st.state, st.have_bytes

    # --- 入口点 2：收编 -------------------------------------------------

    def acquire(self, source: Optional[str] = None) -> AcquireReport:
        """把已有的权重收编进来，不重新下载 2.6 GB。

        `source=None` → 去 HuggingFace / ModelScope 的标准缓存里找
        `source=<目录>` → 扫用户**明确指定**的目录（不主动扫 home：整个
        home 扫一遍既慢又吵）

        两路走同一个顺序，且**顺序不可换**：先按 sha256 验源，通过了才动
        目标位置 —— 反过来做，一次误操作就能把好文件覆盖成坏文件。

        收编成功后**顺带落协议全文**（网易协议 3.4b 要求每份副本都保留协议
        副本），调用方不需要、也不应该再单独调一次。

        `source` 不是目录 → `SourceError`（接口层转 400）。目录在但一个都
        没认出来 → 返回的 `outcomes` 里 `found` 全为 False，**不抛异常**：
        "没找到"是结果，不是错误。
        """
        if source is None:
            hits = discovery.find_all()
            by_key = {k: v for k, v in hits.items()}
        else:
            if not os.path.isdir(source):
                raise SourceError(f"目录不存在：{source}")
            by_key = {f.key: {"path": f.path, "source": "指定目录"}
                      for f in downloader.scan_directory(source)}

        outcomes = []
        for asset in registry.ASSETS:
            spec = downloader.spec_for(asset, self.root)
            hit = by_key.get(asset.key)
            size_text = discovery.human_size(spec.size_bytes)
            if not hit:
                outcomes.append(AdoptOutcome(
                    key=asset.key, label=asset.label,
                    size_bytes=spec.size_bytes, size_text=size_text,
                    found=False, ok=False,
                    error=("标准缓存里没找到" if source is None
                           else "指定目录里没找到")))
                continue
            res = downloader.adopt(spec, hit["path"])
            # 收编进来就是"新的文件"，之前的校验记录作废
            self._verified.pop(asset.key, None)
            outcomes.append(AdoptOutcome(
                key=asset.key, label=asset.label,
                size_bytes=spec.size_bytes, size_text=size_text,
                found=True, ok=res.ok, linked=res.linked,
                skipped=res.skipped, source=hit.get("source", ""),
                source_path=hit["path"], error=res.error))

        if any(o.ok for o in outcomes):
            downloader.copy_license_files(self.root)

        return AcquireReport(source=(source or "标准缓存"),
                             outcomes=tuple(outcomes),
                             report=self.inspect())

    # --- 入口点 3：下载 -------------------------------------------------

    def download(self) -> Task:
        """启动（或排队）后台下载，立即返回句柄。

        幂等：已就绪的会被权重跳过，重复调用只是再排一次队。失败不会在
        调用线程抛 —— 落在 `progress()["error"]` 与逐项 `state="failed"` 上。
        """
        return Task(id=self._manager.start(), _manager=self._manager)
