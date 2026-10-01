"""E2E 骨架自身的测试：素材清单、服务隔离、派生产物一致性

这些不是业务用例，而是"保证 E2E 能跑"的护栏。
最关键的一条：**服务夹具绝不能读写开发者自己的 ~/.vidsub**。
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "tools"))

import e2e_server as es       # noqa: E402
import fixture as fx          # noqa: E402


# --- 素材清单的自洽性 ---------------------------------------------------

def test_fixture_manifest_is_self_consistent():
    """URL 固定、哈希是合法 sha256、裁剪参数自洽且落在 2~3 分钟"""
    assert fx.SOURCE_URL.startswith("https://")
    assert len(fx.EXPECTED_SHA256) == 64
    assert all(c in "0123456789ABCDEF" for c in fx.EXPECTED_SHA256)
    assert fx.TRIM_START + fx.TRIM_SECONDS <= fx.EXPECTED_SECONDS
    assert 120 <= fx.TRIM_SECONDS <= 180


def test_fixture_attribution_present():
    """公有领域素材也必须标注出处"""
    assert "Wikimedia" in fx.ATTRIBUTION
    assert "public domain" in fx.ATTRIBUTION


def test_speech_ratio_floor_is_declared():
    """语音占比下限必须存在且合理（背景音乐会让 VAD 失效）"""
    assert 0.0 < fx.MIN_SPEECH_RATIO <= 0.6


def test_sha256_helper_matches_hashlib(tmp_path):
    import hashlib
    p = tmp_path / "x.bin"
    p.write_bytes(b"hello world")
    assert fx._sha256(str(p)) == hashlib.sha256(b"hello world").hexdigest().upper()


# --- 隔离保证（这一组会真的起服务）---------------------------------------

@pytest.mark.e2e_support
def test_server_fixture_writes_record_into_isolated_dir_only():
    """服务跑起来后，记录文件必须落在临时目录，且 ~/.vidsub 不得被创建。

    上一版只断言"临时目录不等于 ~/.vidsub"——那是 mkdtemp 本身的性质，
    永远为真，等于什么都没验。真正要验的是**副作用的落点**。
    """
    home = os.path.expanduser("~")
    real = os.path.join(home, ".vidsub")
    existed_before = os.path.exists(real)

    with es.running_server() as ctx:
        record = os.path.join(ctx["data_dir"], "instance.json")
        assert os.path.exists(record), "记录文件没落在隔离目录里"
        # 隔离目录之外的常见落点都不该出现
        assert not os.path.exists(os.path.join(ctx["data_dir"], "..", ".vidsub"))

    # 服务跑完、进程退出后，用户目录仍不应被创建
    assert os.path.exists(real) == existed_before, \
        f"E2E 在用户目录里创建了 {real}"


@pytest.mark.e2e_support
def test_server_fixture_uses_env_var_for_data_dir():
    """确认 VIDSUB_DATA_DIR 真的被读取（重构时若丢掉这个读取会静默失效）"""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    import tempfile
    tmp = tempfile.mkdtemp(prefix="vidsub-iso-")
    with es.running_server(port=port, data_dir=tmp) as ctx:
        assert ctx["data_dir"] == tmp
        assert os.path.exists(os.path.join(tmp, "instance.json"))


@pytest.mark.e2e_support
def test_server_fixture_cleans_up_process():
    """退出夹具后服务必须真的停止，且端口可再被占用"""
    import socket
    with es.running_server() as ctx:
        port, pid = ctx["port"], ctx["pid"]
    assert not es.is_up(port, timeout=1.0)
    # 光看端口不够：进程可能关了套接字却还活着
    assert not _pid_alive(pid), "服务进程仍然存活"
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


@pytest.mark.e2e_support
def test_server_fixture_removes_its_temp_dir():
    """自己创建的临时目录要清理，否则每次跑都堆积一个"""
    with es.running_server() as ctx:
        owned = ctx["data_dir"]
    assert not os.path.exists(owned), f"临时目录没被清理：{owned}"


def test_server_fixture_keeps_caller_owned_dir():
    """调用方指定的目录不该被删（那是别人的）"""
    import tempfile
    mine = tempfile.mkdtemp(prefix="vidsub-mine-")
    try:
        with es.running_server(data_dir=mine) as ctx:
            assert ctx["data_dir"] == mine
        assert os.path.isdir(mine)
    finally:
        import shutil
        shutil.rmtree(mine, ignore_errors=True)


def test_launcher_respects_no_browser_flag(monkeypatch):
    """VIDSUB_NO_BROWSER 时不得调用 webbrowser.open

    E2E 每起一个服务就弹一次真实浏览器，既吵又可能让测试挂在浏览器上。
    """
    from vidsub import launcher
    opened = []
    monkeypatch.setattr(launcher.webbrowser, "open",
                        lambda url: opened.append(url))
    monkeypatch.setenv("VIDSUB_NO_BROWSER", "1")

    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    httpd = launcher.make_probe_server(port)
    import threading
    launcher.open_browser_after_ready(
        port, ready=lambda: threading.Thread(
            target=httpd.serve_forever, daemon=True).start())
    time.sleep(1.0)
    httpd.shutdown(); httpd.server_close()
    assert not opened, "设置了 VIDSUB_NO_BROWSER 仍打开了浏览器"


def test_launcher_opens_browser_without_flag(monkeypatch):
    """不加标志时仍应正常打开（确认上一条不是因为逻辑坏了才通过）"""
    from vidsub import launcher
    opened = []
    monkeypatch.setattr(launcher.webbrowser, "open",
                        lambda url: opened.append(url))
    monkeypatch.delenv("VIDSUB_NO_BROWSER", raising=False)

    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    httpd = launcher.make_probe_server(port)
    import threading
    launcher.open_browser_after_ready(
        port, ready=lambda: threading.Thread(
            target=httpd.serve_forever, daemon=True).start())
    deadline = time.time() + 5
    while not opened and time.time() < deadline:
        time.sleep(0.05)
    httpd.shutdown(); httpd.server_close()
    assert opened, "未设置标志时反而没打开浏览器"


def _pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                         capture_output=True, text=True, errors="replace").stdout
    return str(pid) in out and "no tasks" not in out.lower()


# --- 派生产物：参数变了必须重建 ------------------------------------------

def test_manifest_invalidated_when_trim_params_change(tmp_path, monkeypatch):
    """清单不匹配时必须重建，不能静默复用旧产物。"""
    dest = tmp_path / "fx.mp4"
    dest.write_bytes(b"stale")
    (tmp_path / "fx.mp4.manifest").write_text("完全不同的参数")

    rebuilt = {}
    monkeypatch.setattr(fx, "download_source", lambda *a, **k: "unused")

    def _fake_ffmpeg(cmd, **kw):
        # 造出与旧产物不同的内容，并写好清单，模拟"确实重建了"
        Path(cmd[-1]).write_bytes(b"fresh")
        rebuilt["called"] = True
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(fx.subprocess, "run", _fake_ffmpeg)
    monkeypatch.setattr(fx, "_verify_speech", lambda p: None)

    out = fx.build_fixture(dest=str(dest))
    assert rebuilt.get("called"), "清单不匹配却没有触发重建"
    assert out == str(dest)
    assert dest.read_bytes() == b"fresh"
    # 清单应已被更新为当前参数
    stamp = (tmp_path / "fx.mp4.manifest").read_text(encoding="utf-8")
    assert str(fx.TRIM_SECONDS) in stamp


def test_manifest_match_skips_rebuild(tmp_path, monkeypatch):
    """清单匹配且文件在 → 直接复用，不该再调 ffmpeg"""
    dest = tmp_path / "fx.mp4"
    dest.write_bytes(b"cached")
    want = f"{fx.SOURCE_URL}|{fx.EXPECTED_SHA256}|{fx.TRIM_START}|{fx.TRIM_SECONDS}"
    (tmp_path / "fx.mp4.manifest").write_text(want, encoding="utf-8")

    def _boom(*a, **k):
        raise AssertionError("不该重建")

    monkeypatch.setattr(fx.subprocess, "run", _boom)
    assert fx.build_fixture(dest=str(dest)) == str(dest)


def test_free_port_is_bindable():
    import socket
    p = es.free_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", p))
