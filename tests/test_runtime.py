"""推理运行时：两个 llama-server 常驻服务的生命周期

要覆盖的行为（每条都对应工单 03 的一条验收）：

- 串行启动、逐个等就绪 —— 两个模型同时加载 2.5GB 时 `mlock` 会抢页锁而失败
- 第一个作业结束后服务**不退出**，第二个直接复用（不重复加载模型）
- 启动日志**落盘**，起不来时能看到原因
- 进程中途退出要**立刻**上报，不能干等超时
- 超时要上报，不能无限等
- 传给推理进程的模型路径**不含空格**（含空格的绝对路径会让 llama-server 拒绝启动）
- 本地健康检查**不被系统代理劫持**
- 空闲超时自动退出
- 退出后**无残留子进程**

用 `tools/fake_llama_server.py` 当替身：它认同样的参数、同样服务 `/health`，
并且**同样拒绝含空格的模型路径** —— 这条不模仿到位，"我们没传空格"就测不出来。
"""
import json
import os
import socket
import subprocess
import sys
import time

import pytest

from vidsub import registry, runtime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE = os.path.join(ROOT, "tools", "fake_llama_server.py")


# --- 替身本身要先是对的 -------------------------------------------------

def test_fake_double_rejects_model_path_with_space(tmp_path):
    """先确认替身真的学到了"含空格就拒绝"，否则下面的测试都是空跑"""
    m = tmp_path / "my model.gguf"
    m.write_bytes(b"x")
    r = subprocess.run(
        [sys.executable, FAKE, "-m", str(m)],
        capture_output=True, text=True, timeout=30)
    assert r.returncode != 0
    assert "space" in (r.stdout + r.stderr).lower()


def test_fake_double_serves_health(tmp_path):
    m = tmp_path / "m.gguf"
    m.write_bytes(b"x")
    port = _free_port()
    p = subprocess.Popen(
        [sys.executable, FAKE, "--host", "127.0.0.1", "--port", str(port),
         "-m", str(m)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert _wait_health(port, 15), "替身没能提供 /health"
    finally:
        p.kill()


# --- 夹具 ---------------------------------------------------------------

A_GGUF = "r2t2/a.gguf"
B_GGUF = "r2t2/b.gguf"
MT_GGUF = "hy-mt2/b.gguf"


@pytest.fixture
def models(tmp_path):
    """按真实清单的相对布局放一份假权重（体积很小）"""
    root = tmp_path / "models"
    for a in registry.ASSETS:
        p = root / registry.rel_path(a).replace("/", os.sep)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\0" * 64)
    for rel in (A_GGUF, B_GGUF, MT_GGUF):     # 测试用的短名
        p = root / rel.replace("/", os.sep)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\0" * 64)
    return str(root)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_health(port: int, timeout: float) -> bool:
    import urllib.request
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with op.open(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                if b'"ok"' in r.read():
                    return True
        except Exception:
            time.sleep(0.1)
    return False


@pytest.fixture
def rt(models, tmp_path):
    """一个指向替身的 Runtime，用完保证收干净"""
    r = runtime.Runtime(
        model_root=models,
        bin_argv=[sys.executable, FAKE],
        log_dir=str(tmp_path / "logs"),
        startup_timeout=20.0,
        idle_timeout=0.0,
    )
    yield r
    r.stop_all()


def _spec(key, port, model_rel, extra=None):
    return runtime.ServiceSpec(key=key, label=key, port=port,
                               model_rel=model_rel, extra=extra or [])


# --- 串行启动 -----------------------------------------------------------

def test_starts_both_services(rt):
    st = rt.ensure_started([_spec("asr", _free_port(), A_GGUF),
                            _spec("mt", _free_port(), MT_GGUF)])
    assert all(s["state"] == "ready" for s in st), st


def test_second_service_starts_only_after_first_is_ready(rt, monkeypatch):
    """并发加载 2.5GB 时 mlock 会抢不到页锁 —— 必须串行。

    记录每个服务被启动的时刻，断言第二个开始时第一个已经就绪。
    """
    order: list[tuple[str, float]] = []
    real_launch = runtime.Runtime._launch

    def _spy(self, run, *a, **kw):
        order.append((run.spec.key, time.time()))
        return real_launch(self, run, *a, **kw)

    monkeypatch.setattr(runtime.Runtime, "_launch", _spy)

    rt.ensure_started([_spec("asr", _free_port(), A_GGUF),
                       _spec("mt", _free_port(), MT_GGUF)])

    assert [k for k, _ in order] == ["asr", "mt"], "启动顺序不对"
    gap = order[1][1] - order[0][1]
    # 替身默认加载 0.2s；重叠就说明没等第一个就绪
    assert gap >= 0.15, f"第二个服务在 {gap:.2f}s 就启动了，没等第一个就绪"


def test_reports_which_service_failed(rt, monkeypatch):
    """起不来要能指认是哪个，不能只说"启动失败\""""
    monkeypatch.setenv("FAKE_FAIL", "exit")
    with pytest.raises(runtime.StartupError) as ei:
        rt.ensure_started([_spec("asr", _free_port(), A_GGUF)])
    assert "asr" in str(ei.value)


# --- 复用 ---------------------------------------------------------------

def test_reused_across_calls_without_reloading(rt):
    """常驻复用的核心：第二次调用不该再起进程"""
    specs = [_spec("asr", _free_port(), A_GGUF)]
    first = rt.ensure_started(specs)
    pid1 = first[0]["pid"]

    second = rt.ensure_started(specs)
    assert second[0]["pid"] == pid1, "第二次调用重启了服务 —— 白白再加载一遍模型"
    assert second[0]["state"] == "ready"


def test_reuse_survives_many_calls(rt):
    specs = [_spec("asr", _free_port(), A_GGUF)]
    pids = {rt.ensure_started(specs)[0]["pid"] for _ in range(4)}
    assert len(pids) == 1


def test_status_reports_running_service(rt):
    specs = [_spec("asr", _free_port(), A_GGUF)]
    rt.ensure_started(specs)
    st = rt.status()[0]
    assert st["state"] == "ready"
    assert st["port"] > 0
    assert os.path.exists(st["log_path"]), "日志文件不存在"


def test_status_when_nothing_started(rt):
    assert all(s["state"] == "stopped" for s in rt.status())


# --- 日志落盘 -----------------------------------------------------------

def test_startup_log_written_to_disk(rt):
    """起不来时完全无从排查 —— 日志必须落盘"""
    specs = [_spec("asr", _free_port(), A_GGUF)]
    rt.ensure_started(specs)
    log = rt.status()[0]["log_path"]
    assert os.path.exists(log)
    body = open(log, "r", encoding="utf-8", errors="replace").read()
    assert "listening" in body, f"日志里没有启动痕迹：{body[:200]}"


def test_log_tail_included_in_error(rt, monkeypatch):
    """失败信息里要带日志尾巴，否则用户只看到"启动失败"无从下手"""
    monkeypatch.setenv("FAKE_FAIL", "exit")
    with pytest.raises(runtime.StartupError) as ei:
        rt.ensure_started([_spec("asr", _free_port(), A_GGUF)])
    msg = str(ei.value)
    assert "mlock" in msg or "page lock" in msg, f"错误里没带日志尾巴：{msg}"


def test_log_path_has_no_spaces(rt, models):
    """日志路径含空格会让排查时命令难用"""
    for s in rt.status() or []:
        assert " " not in s["log_path"]


# --- 失败与超时 ---------------------------------------------------------

def test_detects_process_exit_immediately(rt, monkeypatch):
    """进程退出要立刻报错，不能干等满超时"""
    monkeypatch.setenv("FAKE_FAIL", "exit")
    t0 = time.time()
    with pytest.raises(runtime.StartupError):
        rt.ensure_started([_spec("asr", _free_port(), A_GGUF)])
    assert time.time() - t0 < 15, "进程早退了却等了很久才报错"


def test_reports_timeout_without_hanging_forever(models, tmp_path):
    """hang 的进程必须超时上报，不能无限等"""
    os.environ["FAKE_FAIL"] = "hang"
    try:
        r = runtime.Runtime(model_root=models,
                            bin_argv=[sys.executable, FAKE],
                            log_dir=str(tmp_path / "logs2"),
                            startup_timeout=2.0, idle_timeout=0.0)
        t0 = time.time()
        with pytest.raises(runtime.StartupError) as ei:
            r.ensure_started([_spec("asr", _free_port(), A_GGUF)])
        assert time.time() - t0 < 12, "超时没生效"
        assert "超时" in str(ei.value) or "timeout" in str(ei.value).lower()
    finally:
        os.environ.pop("FAKE_FAIL", None)
        r.stop_all()


def test_missing_model_file_is_reported_clearly(models, tmp_path):
    """模型不在 → 替身立刻退出，要能报出是哪个模型路径"""
    r = runtime.Runtime(model_root=models, bin_argv=[sys.executable, FAKE],
                        log_dir=str(tmp_path / "logs3"),
                        startup_timeout=15.0, idle_timeout=0.0)
    try:
        with pytest.raises(runtime.StartupError) as ei:
            r.ensure_started([_spec("asr", _free_port(), "r2t2/nope.gguf")])
        assert "nope.gguf" in str(ei.value)
    finally:
        r.stop_all()


# --- 路径不含空格 -------------------------------------------------------

def test_model_path_passed_relative_and_has_no_space(models, tmp_path):
    """核心约束：绝对路径含空格会让 llama-server 拒绝启动。

    本机项目目录就叫 "workbuddy en"（带空格），踩过这个坑。
    """
    seen = {}

    r = runtime.Runtime(model_root=models, bin_argv=[sys.executable, FAKE],
                        log_dir=str(tmp_path / "logs4"),
                        startup_timeout=5.0, idle_timeout=0.0)
    original = r._launch

    def _capture(run, *a, **kw):
        seen[run.spec.key] = (r._cwd, r._argv_for(run.spec))
        return original(run, *a, **kw)

    r._launch = _capture
    try:
        r.ensure_started([_spec("asr", _free_port(), A_GGUF,
                                extra=["--mmproj", B_GGUF])])
    finally:
        r.stop_all()

    cwd, argv = seen["asr"]
    # 只有**模型路径**不能含空格；可执行文件路径含空格没关系
    # （Windows CreateProcess 自己处理），实测踩坑的是 llama-server
    # 解析模型参数那一步。所以只查 -m / --mmproj 的值。
    model = argv[argv.index("-m") + 1]
    assert not os.path.isabs(model), f"模型路径应传相对路径，却传了 {model}"
    assert " " not in model, f"模型路径含空格：{model!r}"
    assert "--mmproj" in argv
    mm = argv[argv.index("--mmproj") + 1]
    assert " " not in mm and not os.path.isabs(mm), f"mmproj 路径不合格：{mm!r}"
    assert " " not in cwd, f"cwd 含空格：{cwd}"
    assert os.path.samefile(cwd, models), "cwd 必须指向模型根，相对路径才解析得到"


def test_refuses_to_start_when_model_root_has_spaces(tmp_path):
    """模型根目录含空格时明确报错，而不是让 llama-server 神秘失败"""
    bad = tmp_path / "has space"
    bad.mkdir()
    r = runtime.Runtime(model_root=str(bad), bin_argv=[sys.executable, FAKE],
                        log_dir=str(tmp_path / "logs5"),
                        startup_timeout=2.0, idle_timeout=0.0)
    with pytest.raises(runtime.SpaceInPathError):
        r.ensure_started([_spec("asr", _free_port(), A_GGUF)])


def test_space_free_default_root_avoids_username_spaces(monkeypatch):
    """用户名带空格时，默认数据目录要躲开 —— 否则默认安装就起不来"""
    fake_home = "C:/Users/John Smith"
    monkeypatch.setenv("VIDSUB_DATA_DIR", "")
    monkeypatch.delenv("VIDSUB_DATA_DIR", raising=False)
    got = runtime.default_model_root(home=fake_home)
    assert " " not in got, f"默认目录含空格：{got}"


# --- 代理不被劫持 -------------------------------------------------------

def test_local_health_checks_bypass_system_proxy(rt, monkeypatch):
    """http_proxy 会劫持 localhost 请求，导致健康检查永远失败"""
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    st = rt.ensure_started([_spec("asr", _free_port(), A_GGUF)])
    assert st[0]["state"] == "ready", "被系统代理劫持了"


def test_child_process_env_has_no_proxy(rt):
    """推理进程也不该继承代理设置"""
    specs = [_spec("asr", _free_port(), A_GGUF)]
    rt.ensure_started(specs)
    env = rt._child_env()
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        assert k not in env or not env[k], f"{k} 被传给了推理进程"


# --- 空闲超时 -----------------------------------------------------------

def test_idle_timeout_stops_service(models, tmp_path):
    """空闲超时自动退出，别长期占着几 GB 内存"""
    r = runtime.Runtime(model_root=models, bin_argv=[sys.executable, FAKE],
                        log_dir=str(tmp_path / "logs6"),
                        startup_timeout=15.0, idle_timeout=1.0)
    spec = _spec("asr", _free_port(), A_GGUF)
    st = r.ensure_started([spec])
    pid = st[0]["pid"]
    try:
        deadline = time.time() + 15
        while time.time() < deadline and _alive(pid):
            time.sleep(0.3)
        assert not _alive(pid), "空闲超时后进程还活着，白占内存"
    finally:
        r.stop_all()


def test_activity_defers_idle_shutdown(models, tmp_path):
    """有作业在跑时不能被空闲回收"""
    r = runtime.Runtime(model_root=models, bin_argv=[sys.executable, FAKE],
                        log_dir=str(tmp_path / "logs7"),
                        startup_timeout=15.0, idle_timeout=1.5)
    spec = _spec("asr", _free_port(), A_GGUF)
    pid = r.ensure_started([spec])[0]["pid"]
    try:
        for _ in range(4):          # 持续标记"在用"
            r.touch()
            time.sleep(0.5)
        assert _alive(pid), "有活动时不该被空闲回收"
    finally:
        r.stop_all()


def test_idle_disabled_when_zero(models, tmp_path):
    """idle_timeout=0 表示不自动退出（测试与长任务场景）"""
    r = runtime.Runtime(model_root=models, bin_argv=[sys.executable, FAKE],
                        log_dir=str(tmp_path / "logs8"),
                        startup_timeout=15.0, idle_timeout=0.0)
    pid = r.ensure_started([_spec("asr", _free_port(), A_GGUF)])[0]["pid"]
    try:
        time.sleep(2.5)
        assert _alive(pid)
    finally:
        r.stop_all()


# --- 退出清理 -----------------------------------------------------------

def test_stop_all_leaves_no_child_process(rt):
    st = rt.ensure_started([_spec("asr", _free_port(), A_GGUF),
                            _spec("mt", _free_port(), MT_GGUF)])
    pids = [s["pid"] for s in st]
    assert all(_alive(p) for p in pids)

    rt.stop_all()
    for p in pids:
        deadline = time.time() + 10
        while time.time() < deadline and _alive(p):
            time.sleep(0.2)
        assert not _alive(p), f"pid {p} 残留 —— 会白占内存"


def test_stop_kills_descendants_not_just_direct_children(tmp_path):
    """必须按 pid 连子树一起杀。

    Windows 上 detached 起的子进程不随父进程退出，会残留成孤儿；
    单杀直接子进程，llama-server 自己 fork 出来的还是会留着。

    孙进程的 pid 让它**自己写到文件**里 —— 早先靠 `wmic` 查 ParentProcessId，
    那东西在新版 Windows 上已经没了，测试直接 FileNotFoundError。
    """
    pidfile = tmp_path / "grandchild.pid"
    grandchild_src = tmp_path / "grandchild.py"
    grandchild_src.write_text(
        "import os, sys, time\n"
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(300)\n", encoding="utf-8")
    spawner = tmp_path / "spawner.py"
    spawner.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(grandchild_src)!r}])\n"
        "time.sleep(300)\n", encoding="utf-8")

    parent = subprocess.Popen([sys.executable, str(spawner)],
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline and not pidfile.exists():
        time.sleep(0.2)
    assert pidfile.exists(), "孙进程没起来"
    grandchild = int(pidfile.read_text().strip())
    try:
        assert _alive(grandchild)

        runtime.kill_tree(parent.pid)
        deadline = time.time() + 15
        while time.time() < deadline and (_alive(parent.pid)
                                           or _alive(grandchild)):
            time.sleep(0.2)
        assert not _alive(grandchild), "孙进程残留 —— 只杀了直接子进程"
    finally:
        for p in (parent.pid, grandchild):
            if _alive(p):
                _kill_tree(p)


def test_stop_is_idempotent(rt):
    rt.ensure_started([_spec("asr", _free_port(), A_GGUF)])
    rt.stop_all()
    rt.stop_all()          # 再来一次不能抛


def test_stop_then_restart_works(rt):
    spec = _spec("asr", _free_port(), A_GGUF)
    pid1 = rt.ensure_started([spec])[0]["pid"]
    rt.stop_all()
    pid2 = rt.ensure_started([spec])[0]["pid"]
    assert pid2 != pid1, "停掉之后又复用了一个已死的 pid"
    assert _alive(pid2)


# --- 服务定义 -----------------------------------------------------------

def test_default_services_match_verified_pipeline():
    """服务定义要与 r2t2-test 里实测通过的那套一致

    识别要带 mmproj 与 -c 4096；翻译要 --jinja 与 -c 1024。
    参数改错了不会立刻报错，而是输出退化 —— 很难在事后看出来。
    """
    by = {s.key: s for s in runtime.default_services()}
    assert set(by) == {"asr", "mt"}

    asr, mt = by["asr"], by["mt"]
    assert "--mmproj" in asr.extra, "识别服务缺 mmproj：没有它读不了音频"
    assert "r2t2" in asr.model_rel
    assert "4096" in asr.extra, "识别上下文应 4096（实测值）"
    assert "-m" not in asr.extra, "-m 由 model_rel 隐式给出，不该再写一遍"

    assert "--jinja" in mt.extra, "翻译服务缺 --jinja，模板不会生效"
    assert "hy-mt2" in mt.model_rel
    assert "1024" in mt.extra, "翻译上下文应 1024（实测值）"


def test_services_use_distinct_ports():
    ports = [s.port for s in runtime.default_services()]
    assert len(ports) == len(set(ports)), f"两个服务端口撞了：{ports}"


def test_services_bind_loopback_only():
    """只监听 127.0.0.1，不要 0.0.0.0"""
    for s in runtime.default_services():
        assert s.host == "127.0.0.1", f"{s.key} 监听了 {s.host}"


def test_service_model_paths_match_registry():
    """服务里的模型相对路径要跟清单对得上，否则指向不存在的文件"""
    for s in runtime.default_services():
        key = {"asr": "asr_model", "mt": "mt_model"}[s.key]
        want = registry.rel_path(registry.get(key))
        assert s.model_rel == want, \
            f"{s.key}: 服务写的是 {s.model_rel}，清单是 {want}"


def test_asr_mmproj_matches_registry():
    asr = next(s for s in runtime.default_services() if s.key == "asr")
    want = registry.rel_path(registry.get("asr_mmproj"))
    assert asr.extra[asr.extra.index("--mmproj") + 1] == want


# --- 二进制定位 ---------------------------------------------------------

def test_finds_llama_server_in_data_bin_dir(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    exe = d / ("llama-server.exe" if os.name == "nt" else "llama-server")
    exe.write_bytes(b"MZ" if os.name == "nt" else b"x")
    exe.chmod(0o755)
    got = runtime.find_llama_server(bin_dir=str(d))
    assert got and os.path.isfile(got)


def test_missing_binary_reports_what_it_looked_for(tmp_path):
    """二进制找不到要说清找过哪儿，而不是抛 FileNotFoundError"""
    with pytest.raises(runtime.RuntimeMissingError) as ei:
        runtime.find_llama_server(bin_dir=str(tmp_path / "nope"))
    msg = str(ei.value)
    assert "VIDSUB_LLAMA_SERVER" in msg, "没告诉用户可以怎么指定"
    assert "llama-server" in msg


def test_env_var_overrides_bin_dir(tmp_path, monkeypatch):
    d = tmp_path / "custom"
    d.mkdir()
    exe = d / ("llama-server.exe" if os.name == "nt" else "llama-server")
    exe.write_bytes(b"x")
    exe.chmod(0o755)
    monkeypatch.setenv("VIDSUB_LLAMA_SERVER", str(exe))
    got = runtime.find_llama_server(bin_dir=str(tmp_path / "nope"))
    assert os.path.samefile(got, str(exe))


def test_binary_name_is_platform_appropriate(tmp_path):
    d = tmp_path / "b"
    d.mkdir()
    name = "llama-server.exe" if os.name == "nt" else "llama-server"
    (d / name).write_bytes(b"x")
    (d / name).chmod(0o755)
    assert runtime.find_llama_server(bin_dir=str(d))


# --- 辅助 ---------------------------------------------------------------

def _alive(pid: int) -> bool:
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


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True)
    else:
        subprocess.run(["pkill", "-9", "-P", str(pid)], capture_output=True)
        try:
            os.kill(pid, 9)
        except OSError:
            pass




def test_json_status_is_serialisable(rt):
    """状态要给 HTTP 接口用，必须能序列化成 JSON"""
    rt.ensure_started([_spec("asr", _free_port(), A_GGUF)])
    json.dumps(rt.status())