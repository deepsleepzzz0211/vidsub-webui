"""推理运行时：两个 llama-server 常驻服务的生命周期

沿用 r2t2-test 里实测通过的那套配置（参数改错了不会立刻报错，只会让输出
悄悄退化，事后很难看出来，所以定义写死在这里并有测试守着）。

三个方向相反、都要处理的坑：

1. **路径含空格**：llama-server 收到含空格的模型绝对路径会报 `invalid
   argument` 直接拒绝启动。本机项目目录就叫 "workbuddy en"（带空格），
   实测踩过。做法：模型路径传**相对路径** + `cwd` 指到模型根。
   注意**可执行文件路径含空格没关系**（Windows CreateProcess 自己处理），
   有问题的只是它自己解析的那个模型参数。
2. **必须串行启动**：两个模型同时加载 2.5GB 时 `--load-mode mlock` 抢不到
   页锁会失败。所以逐个起、等就绪再起下一个。
3. **退出残留**：Windows 上 detached 起的子进程不随父进程退出，会残留成
   孤儿、白占几 GB。要按 pid 连子树一起杀。

另外两条容易漏的：
- **日志必须落盘**。丢给 DEVNULL 的话，服务起不来时完全无从排查。
- **本地请求不能走系统代理**。`http_proxy` 会劫持 localhost，健康检查永远失败。
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from . import registry

# 本地健康检查专用：显式清空代理，否则会捡起 http_proxy / 系统设置，
# 把 127.0.0.1 的请求绕到代理上去（实测踩过：健康检查永远不 ready）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

IDLE_ENV = "VIDSUB_IDLE_TIMEOUT"
BIN_ENV = "VIDSUB_LLAMA_SERVER"


class RuntimeError_(RuntimeError):
    """本模块所有异常的根。留着是为了让 except RuntimeError_ 一次抓全。"""


class StartupError(RuntimeError_):
    """服务起不来。消息里带日志尾巴与具体是哪个服务。"""


class SpaceInPathError(StartupError):
    """模型路径含空格 —— llama-server 会拒绝启动，不如早点说清楚"""


class RuntimeMissingError(StartupError):
    """找不到 llama-server 可执行文件。

    归到 StartupError 下面：它本来就是"服务起不来"的一种，接口层按
    StartupError 统一转成 503 + 可读消息，不用再单独开一个 except 分支
    —— 漏掉分支的代价是用户看到一个 500 Internal Server Error。
    """


# 服务状态。用常量而不是裸字符串：字面量在代码里散着十几处，
# 拼错一个不会报错，只会让那个状态永远匹配不上。
STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_READY = "ready"
STATE_STOPPING = "stopping"
STATE_FAILED = "failed"


@dataclass(frozen=True)
class ServiceSpec:
    key: str
    label: str
    port: int
    model_rel: str                      # 相对模型根，不含空格
    extra: tuple = ()                   # 其余参数，不含 -m
    host: str = "127.0.0.1"            # 只监听回环


# service 的绝对路径在 Runtime 里拼（它才知道 model_root），所以这里只存相对路径


def _common_args() -> list:
    """r2t2-test 实测的最优值，别随手改

    -ngl 0        纯 CPU（实测 GPU 反而更慢，且要求驱动）
    -t 8          8 线程已是最优：4→8 几乎无变化，说明受内存带宽限制
    --parallel 1  单并发；我们要串行推理，开大只会抢页锁
    --load-mode mlock  锁住权重防换页
    """
    return ["-ngl", "0", "-t", "8", "--parallel", "1",
            "--load-mode", "mlock", "-fa", "auto", "--prio", "2"]


def default_services() -> list:
    """识别 + 翻译两个服务。参数与实测通过的流水线一致。"""
    def rel(key: str) -> str:
        return registry.rel_path(registry.get(key))

    return [
        ServiceSpec(
            key="asr", label="语音识别", port=8081,
            model_rel=rel("asr_model"),
            # 识别必须带 mmproj：没有它读不了音频输入
            extra=("--mmproj", rel("asr_mmproj"), "-c", "4096"),
        ),
        ServiceSpec(
            key="mt", label="翻译", port=8082,
            model_rel=rel("mt_model"),
            # --jinja 不给的话 chat 模板不生效，翻译输出会退化
            extra=("--jinja", "-c", "1024"),
        ),
    ]


def default_model_root(home: str | None = None) -> str:
    """权重落盘目录。**必须不含空格**。

    用户名带空格时（"John Smith"）`~/.vidsub` 就废了，默认安装直接起不来。
    这时退到一个不含空格的本地路径，而不是等用户来踩。
    """
    base = os.environ.get("VIDSUB_DATA_DIR")
    if base:
        return os.path.join(base, "models")
    home = home or os.path.expanduser("~")
    for cand in (os.path.join(home, ".vidsub"),
                 os.path.join(os.environ.get("LOCALAPPDATA", tempfile_dir()),
                              "vidsub")):
        if " " not in cand:
            return os.path.join(cand, "models")
    return os.path.join(tempfile_dir(), "vidsub-models")


def tempfile_dir() -> str:
    import tempfile
    return tempfile.gettempdir()


def binary_name() -> str:
    return "llama-server.exe" if os.name == "nt" else "llama-server"


def find_llama_server(bin_dir: str = "") -> str:
    """定位 llama-server 可执行文件。

    顺序：环境变量 → 指定目录 → 默认数据目录下的 bin/。
    找不到时把**找过哪些地方**一并报出来，否则用户只能干瞪眼。
    """
    env = os.environ.get(BIN_ENV)
    if env:
        if os.path.isfile(env):
            return env
        raise RuntimeMissingError(
            f"{BIN_ENV} 指向的文件不存在：{env}")

    name = binary_name()
    tried: list[str] = []
    roots = [bin_dir] if bin_dir else []
    roots.append(os.path.join(os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "vendor"))
    roots.append(os.path.join(default_model_root(), "bin"))
    for r in roots:
        if not r:
            continue
        cand = os.path.join(r, name)
        tried.append(cand)
        if os.path.isfile(cand):
            return cand
    raise RuntimeMissingError(
        "找不到 llama-server 可执行文件。找过：\n  "
        + "\n  ".join(tried)
        + f"\n\n解决办法（二选一）：\n"
          f"  1. 设 {BIN_ENV} 指向现有的 llama-server\n"
          f"  2. 把 llama.cpp 的 bin 目录整个拷到上述任一位置")


@dataclass
class Running:
    spec: ServiceSpec
    pid: Optional[int] = None
    state: str = STATE_STOPPED        # stopped | starting | ready | failed
    log_path: str = ""
    error: str = ""
    last_used: float = 0.0
    proc: Optional[subprocess.Popen] = field(default=None, repr=False)


class Runtime:
    """管理两个常驻 llama-server 进程。"""

    def __init__(self, model_root: str = "", bin_argv: Optional[list] = None,
                 log_dir: str = "", startup_timeout: float = 180.0,
                 idle_timeout: float = 1800.0):
        self.model_root = model_root or default_model_root()
        self._bin_argv = list(bin_argv) if bin_argv else None
        self.log_dir = log_dir or os.path.join(
            os.path.dirname(self.model_root), "logs")
        self.startup_timeout = startup_timeout
        self.idle_timeout = idle_timeout
        self._cwd = self.model_root
        self._services: dict[str, Running] = {}
        self._lock = threading.RLock()
        self._stopping = False
        self._reaper: Optional[threading.Thread] = None
        self._holders = 0

    # --- 参数拼装 --------------------------------------------------------

    def _resolve_bin(self) -> list:
        if self._bin_argv:
            return list(self._bin_argv)
        return [find_llama_server(os.path.join(self.model_root, "..", "bin"))]

    def _argv_for(self, spec: ServiceSpec) -> list:
        """完整 argv。

        `-m` 由 ``model_rel`` 隐式给出，**一律相对路径** —— 含空格的绝对
        路径会被 llama-server 拒绝启动。cwd 指向模型根，相对路径才解析得到。
        """
        return (self._resolve_bin()
                + ["--host", spec.host, "--port", str(spec.port)]
                + _common_args()
                + ["-m", spec.model_rel]
                + list(spec.extra))

    def _child_env(self) -> dict:
        """推理进程的环境：剥掉代理变量。

        留着会让 llama-server 去连本地端口时绕一圈代理，
        表现为"服务起来了但健康检查永远不 ready"。
        """
        env = dict(os.environ)
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                  "ALL_PROXY", "all_proxy"):
            env.pop(k, None)
        env["LLAMA_CACHE"] = os.path.join(self.model_root, ".llama-cache")
        return env

    # --- 生命周期 --------------------------------------------------------

    def ensure_started(self, specs: list) -> list:
        """确保这些服务都在跑，**串行**启动、逐个等就绪。

        已在跑的直接复用 —— 这就是"省掉每次重新加载模型"的关键。
        """
        out: list[dict] = []
        for spec in specs:
            out.append(self._ensure_one(spec))
        self._ensure_reaper()
        return out

    def _ensure_reaper(self) -> None:
        """空闲巡检线程该在的时候必须在。

        判据是 **is_alive() 而不是"有没有这个对象"**：stop_all() 会让线程
        正常退出，但 self._reaper 仍指着那个已死对象。只判存在的话，
        「停止 → 再启动」之后巡检就永远不再恢复 —— 而这恰恰是文档里推荐的
        操作流程，结果 4.3 GB 内存一直挂着不回收。
        """
        if self.idle_timeout <= 0:
            return
        if self._reaper is None or not self._reaper.is_alive():
            self.start_idle_reaper()

    def _ensure_one(self, spec: ServiceSpec) -> dict:
        with self._lock:
            self._stopping = False
            run = self._services.setdefault(spec.key, Running(spec=spec))

            if self._healthy(run) and self._alive(run.pid):
                run.state = STATE_READY
                run.last_used = time.time()
                return self._describe(run)

            if " " in self._cwd:
                raise SpaceInPathError(
                    f"模型目录含空格，llama-server 会拒绝启动：\n  {self._cwd}\n"
                    f"含空格的绝对路径会让它报 invalid argument（实测踩过，"
                    f"本机项目目录就叫 \"workbuddy en\"）。\n"
                    f"请设 VIDSUB_DATA_DIR 指到一个不含空格的目录。")

            os.makedirs(self.log_dir, exist_ok=True)
            run.log_path = os.path.join(self.log_dir, f"{spec.key}.log")
            run.state = STATE_STARTING
            run.error = ""
            self._launch(run)
            return self._await_ready(run)

    def _launch(self, run: Running) -> None:
        """起进程。日志必须落盘 —— 丢 DEVNULL 的话起不来完全无从排查。"""
        argv = self._argv_for(run.spec)
        lf = open(run.log_path, "wb")
        try:
            run.proc = subprocess.Popen(
                argv, cwd=self._cwd, stdout=lf, stderr=lf,
                env=self._child_env(),
                # Windows: 不弹控制台窗口。detached 不能用 —— 那样进程不随
                # 父进程退出，会残留成孤儿、白占内存，只能靠 stop_all 收。
                creationflags=(subprocess.CREATE_NO_WINDOW
                               if os.name == "nt" else 0))
            run.pid = run.proc.pid
        finally:
            lf.close()

    def _await_ready(self, run: Running) -> dict:
        """轮询到就绪。要有超时，且进程死了要立刻报错。"""
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self._healthy(run):
                run.state = STATE_READY
                run.last_used = time.time()
                return self._describe(run)
            if not self._alive(run.pid):
                run.state = STATE_FAILED
                run.error = f"进程已退出（pid {run.pid}）"
                raise StartupError(
                    f"[{run.spec.key}] {run.spec.label} 启动失败：{run.error}\n"
                    f"命令行：{' '.join(self._argv_for(run.spec))}\n"
                    f"日志：{run.log_path}\n{tail(run.log_path)}")
            time.sleep(0.25)
        run.state = STATE_FAILED
        run.error = f"等待就绪超过 {self.startup_timeout:.0f}s"
        raise StartupError(
            f"[{run.spec.key}] {run.spec.label} 启动超时"
            f"（等 {self.startup_timeout:.0f}s，端口 {run.spec.port}）：\n"
            f"日志：{run.log_path}\n{tail(run.log_path)}")

    # --- 健康检查 --------------------------------------------------------

    def _healthy(self, run: Running) -> bool:
        if not run.spec.port:
            return False
        url = f"http://{run.spec.host}:{run.spec.port}/health"
        try:
            with _OPENER.open(url, timeout=2) as r:
                return r.status == 200 and b'"ok"' in r.read()
        except Exception:
            return False

    @staticmethod
    def _alive(pid: Optional[int]) -> bool:
        return alive(pid)

    # --- 对外状态 --------------------------------------------------------

    def status(self) -> list:
        with self._lock:
            out = []
            for run in self._services.values():
                if run.state in (STATE_STARTING, STATE_READY) and not self._alive(run.pid):
                    run.state = STATE_STOPPED      # 别把死进程报成 ready
                out.append(self._describe(run))
            return out

    def _describe(self, run: Running) -> dict:
        idle = self.idle_timeout
        since = (time.time() - run.last_used) if run.last_used else None
        return {
            "key": run.spec.key,
            "label": run.spec.label,
            "state": run.state,
            "port": run.spec.port,
            "pid": run.pid,
            "model": run.spec.model_rel,
            "log_path": run.log_path,
            "error": run.error,
            "idle_seconds": round(since, 1) if since is not None else None,
            "idle_timeout": idle,
        }

    def acquire(self) -> None:
        """登记一个"正在用"的持有者（08：作业在跑时服务不许被回收）。

        单靠 touch() 不够 —— 09 号票的作业要跑 20 分钟，touch 只在进度
        回调里打点；万一某一段推理卡住 40 分钟没人打点，空闲巡检照样会把
        正在用的模型杀掉，作业后半程直接连不上。引用计数是硬保证：
        计数不为 0，reap_idle 就不动手。
        """
        with self._lock:
            self._holders += 1
            for run in self._services.values():
                if run.state == STATE_READY:
                    run.last_used = time.time()

    def release(self) -> None:
        with self._lock:
            self._holders = max(0, self._holders - 1)

    @property
    def holders(self) -> int:
        return self._holders

    def touch(self) -> None:
        """标记"正在用"，推迟空闲退出"""
        with self._lock:
            for run in self._services.values():
                if run.state == STATE_READY:
                    run.last_used = time.time()

    def start_idle_reaper(self, interval: float = 0.0) -> threading.Thread:
        """后台巡检：空闲超时就关掉服务。

        "自动退出"得有个东西在驱动，光有个 reap_idle() 方法不算 ——
        没人调用就等于永远不回收，几 GB 内存一直挂着。

        巡检间隔要**跟着空闲超时走**：写死 30 秒的话，超时设 1 分钟没问题，
        但设 5 秒就要等 30 秒才回收，行为跟配置对不上。
        """
        if self._reaper and self._reaper.is_alive():
            return self._reaper
        if interval <= 0:
            interval = max(1.0, min(30.0, self.idle_timeout / 2))

        def _loop():
            while not self._stopping:
                time.sleep(interval)
                try:
                    self.reap_idle()
                except Exception:
                    pass        # 巡检线程绝不能因为一次异常就死掉

        self._reaper = threading.Thread(target=_loop, daemon=True,
                                        name="vidsub-idle-reaper")
        self._reaper.start()
        return self._reaper

    def reap_idle(self) -> list:
        """空闲超时就退出，别长期占着几 GB 内存。返回被关掉的。

        **杀进程必须在锁外**：`taskkill` 有 30 秒超时、`proc.wait` 还有 10 秒，
        锁内做这些会把 `status()` / `touch()` 一起堵住 40 秒 —— 而
        /api/runtime/touch 是作业运行中被高频调用的。顺带也避免了与
        stop_all 同时杀同一个进程（双 taskkill + 并发 Popen.wait）。

        **有持有者时一律不回收**（见 `acquire`）：正在跑的作业不能被
        空闲巡检把模型杀掉。
        """
        if self.idle_timeout <= 0:
            return []
        if self._holders > 0:
            return []
        now = time.time()
        victims: list[Running] = []
        with self._lock:
            for run in self._services.values():
                if run.state != STATE_READY or not run.last_used:
                    continue
                if now - run.last_used >= self.idle_timeout:
                    run.state = "stopping"
                    victims.append(run)
        dead: list[str] = []
        for run in victims:
            self._kill(run)
            dead.append(run.spec.key)
        return dead

    # --- 退出 ------------------------------------------------------------

    def stop_all(self) -> None:
        """按 pid 连子树一起杀。

        单杀直接子进程不够：llama-server 自己还会 fork。Windows 上
        detached 起的进程不随父进程退出，留下来就是白占内存的孤儿。
        """
        with self._lock:
            self._stopping = True
            runs = list(self._services.values())
        for run in runs:
            self._kill(run)

    def _kill(self, run: Running) -> None:
        pid = run.pid
        if not pid:
            run.state = STATE_STOPPED
            return
        kill_tree(pid)
        if run.proc is not None:
            try:
                run.proc.wait(timeout=10)
            except Exception:
                pass
        run.pid = None
        run.proc = None
        run.state = STATE_STOPPED


def tail(path: str, n: int = 20) -> str:
    """日志尾巴。失败时没有它用户完全无从下手。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:]).rstrip()
    except OSError:
        return "（读不到日志）"


def kill_tree(pid: int) -> None:
    """按 pid 连**子树**一起杀。

    必须带 /T（连子进程）与 /F（强杀）：llama-server 自己还会 fork，
    只杀直接子进程的话那些照样留着；而 Windows 上 detached 起的进程不随
    父进程退出，留下来就是白占内存的孤儿。
    """
    if not pid:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=30)
        return
    subprocess.run(["pkill", "-9", "-P", str(pid)], capture_output=True)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def alive(pid: Optional[int]) -> bool:
    """进程是否还活着。不用 psutil：Windows 上 tasklist 就够。"""
    if not pid:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout
        return str(pid) in out and "no tasks" not in out.lower()
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False