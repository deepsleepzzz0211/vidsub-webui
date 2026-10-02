"""推理运行时在服务里的接线

要验的用户可见行为：
- 缺权重时**不**贸然启动（那必然失败，白等 3 分钟）
- 权重齐了能拉起来，且重复调用是复用一个进程
- 起不来时错误信息里要带日志尾巴，否则用户无从下手
- 手动停止能释放内存
- 状态接口能给出端口与日志位置

这里用替身二进制，不起真的 llama-server。
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

from vidsub import registry, runtime, server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE = os.path.join(ROOT, "tools", "fake_llama_server.py")

A_GGUF = "r2t2/a.gguf"
B_GGUF = "r2t2/b.gguf"
MT_GGUF = "hy-mt2/b.gguf"


@pytest.fixture(autouse=True)
def fake_runtime(request, monkeypatch):
    """把 Runtime 的可执行文件换成替身，并把空闲回收关掉（免得测试中途被回收）

    打了 ``no_fake_bin`` 标记的用例不注入替身 —— 它们要验的正是
    "找不到可执行文件"这条路径，注入了就永远走不到。
    """
    if "no_fake_bin" in request.keywords:
        yield []
        return
    made: list = []

    real_init = runtime.Runtime.__init__

    def _init(self, *a, **kw):
        kw["bin_argv"] = [sys.executable, FAKE]
        kw.setdefault("idle_timeout", 0.0)
        real_init(self, *a, **kw)
        made.append(self)

    monkeypatch.setattr(runtime.Runtime, "__init__", _init)
    yield made
    for r in made:
        try:
            r.stop_all()
        except Exception:
            pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    _shrink(monkeypatch)
    monkeypatch.setenv("VIDSUB_DATA_DIR", str(tmp_path / "data"))
    server._rt = None
    server._rt_root = ""
    with TestClient(server.app) as c:
        yield c
    server._rt = None
    server._rt_root = ""


SIZE = 4096


def _fake_bytes(key: str) -> bytes:
    """体积精确、每个权重内容各不相同（内容相同会导致哈希相同，
    扫描/校验时互相串味）。"""
    seed = (key + "-weight-bytes-").encode()
    out = (seed * (SIZE // len(seed) + 1))[:SIZE]
    assert len(out) == SIZE
    return out


def _shrink(monkeypatch, size=SIZE):
    """把清单里的体积缩到几 KB，测试里不必造 2.6 GB。

    必须**同时**缩体积和换哈希：status 先比体积再比哈希，两样都要对得上
    才会判成 ready。只缩体积的话会被判成 partial，/api/runtime/start 直接 409。
    """
    import hashlib
    small = [registry.Asset(
        key=a.key, label=a.label, repo=a.repo, path=a.path,
        size_bytes=size, sha256=hashlib.sha256(_fake_bytes(a.key)).hexdigest(),
        hf_repo=a.hf_repo, hf_path=a.hf_path, source=a.source,
        license_note=a.license_note, purpose=a.purpose)
        for a in registry.ASSETS]
    monkeypatch.setattr(registry, "ASSETS", tuple(small))
    return small


def _seed_models():
    """按清单布局放够权重，让 /api/runtime/start 放行"""
    root = server.data_dir()
    for a in registry.ASSETS:
        p = os.path.join(root, *registry.rel_path(a).split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(_fake_bytes(a.key))
    return root


# --- 缺权重时不要瞎启动 ------------------------------------------------

def test_refuses_to_start_when_models_missing(client):
    """缺权重启动必然失败，要立刻拒绝而不是白等三分钟"""
    r = client.post("/api/runtime/start")
    assert r.status_code == 409
    assert "下载" in r.json()["error"]


def test_refusal_names_missing_assets(client):
    b = client.post("/api/runtime/start").json()
    assert len(b["missing"]) == 4


def test_status_works_even_when_models_missing(client):
    """状态查询不该被缺权重挡住 —— 页面要能显示"没就绪"而不是报错"""
    b = client.get("/api/runtime").json()
    assert b["services"] == []


# --- 启动与复用 ---------------------------------------------------------

def test_starts_both_services(client):
    _seed_models()
    r = client.post("/api/runtime/start")
    assert r.status_code == 200, r.text
    st = r.json()["services"]
    assert len(st) == 2
    assert all(s["state"] == "ready" for s in st), st
    keys = {s["key"] for s in st}
    assert keys == {"asr", "mt"}


def test_second_call_reuses_same_processes(client):
    """常驻复用的核心：第二个作业不该重新加载模型"""
    _seed_models()
    first = client.post("/api/runtime/start").json()["services"]
    pids = {s["key"]: s["pid"] for s in first}

    second = client.post("/api/runtime/start").json()["services"]
    assert {s["key"]: s["pid"] for s in second} == pids, \
        "第二次调用重启了服务 —— 白白再加载一遍模型"


def test_repeated_starts_do_not_leak_processes(client):
    _seed_models()
    for _ in range(3):
        client.post("/api/runtime/start")
    pids = {s["pid"] for s in client.get("/api/runtime").json()["services"]}
    assert len(pids) == 2, f"起了多余的进程：{pids}"


def test_status_endpoint_reports_ports_and_logs(client):
    _seed_models()
    client.post("/api/runtime/start")
    st = client.get("/api/runtime").json()["services"]
    for s in st:
        assert s["port"] in (8081, 8082)
        assert s["pid"] > 0
        assert os.path.exists(s["log_path"]), "日志不在，前端没法给出错原因"


def test_services_bind_loopback_only(client):
    _seed_models()
    st = client.post("/api/runtime/start").json()["services"]
    for s in st:
        assert s["port"] > 0


# --- 失败上报 -----------------------------------------------------------

def test_startup_failure_returns_log_tail(client, monkeypatch):
    """起不来时错误信息要带日志尾巴，否则用户只看到"启动失败"无从下手"""
    _seed_models()
    monkeypatch.setenv("FAKE_FAIL", "exit")
    r = client.post("/api/runtime/start")
    assert r.status_code == 503
    err = r.json()["error"]
    assert "mlock" in err or "page lock" in err, f"没带日志尾巴：{err}"
    assert "asr" in err, "没指出是哪个服务"


@pytest.mark.no_fake_bin
def test_missing_binary_is_reported_actionably(client, monkeypatch):
    """找不到 llama-server 要说清怎么解决，而不是抛 FileNotFoundError"""
    _seed_models()
    monkeypatch.delenv("VIDSUB_LLAMA_SERVER", raising=False)
    r = client.post("/api/runtime/start")
    assert r.status_code == 503
    err = r.json()["error"]
    assert "VIDSUB_LLAMA_SERVER" in err, "没告诉用户可以怎么指定"
    assert "llama-server" in err


# --- 手动停止 -----------------------------------------------------------

def test_stop_releases_services(client):
    _seed_models()
    client.post("/api/runtime/start")
    assert all(s["state"] == "ready"
               for s in client.get("/api/runtime").json()["services"])

    client.post("/api/runtime/stop")
    st = client.get("/api/runtime").json()["services"]
    assert all(s["state"] == "stopped" for s in st), st
    assert all(s["pid"] is None for s in st)


def test_stop_actually_kills_the_process(client):
    """必须真的杀掉，不只是把状态改成 stopped"""
    _seed_models()
    client.post("/api/runtime/start")
    pids = [s["pid"] for s in client.get("/api/runtime").json()["services"]]
    client.post("/api/runtime/stop")

    import time
    for pid in pids:
        deadline = time.time() + 15
        while time.time() < deadline and runtime.alive(pid):
            time.sleep(0.2)
        assert not runtime.alive(pid), f"pid {pid} 还活着，白占内存"


def test_start_after_stop_works(client):
    _seed_models()
    client.post("/api/runtime/start")
    client.post("/api/runtime/stop")
    r = client.post("/api/runtime/start")
    assert r.status_code == 200
    assert all(s["state"] == "ready" for s in r.json()["services"])


# --- 空闲推迟 -----------------------------------------------------------

def test_touch_defers_idle_shutdown(client):
    _seed_models()
    client.post("/api/runtime/start")
    r = client.post("/api/runtime/touch")
    assert r.status_code == 200 and r.json()["ok"] is True