"""启动器的可测逻辑：空闲端口、已有实例探测、浏览器打开时机

这些是本票最容易"静默出错"的地方，所以先把它们变成可测的纯函数/窄接口。
"""
import socket
import threading
import time

from vidsub import launcher


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_pick_port_returns_bindable_port():
    port = launcher.pick_port()
    assert isinstance(port, int) and port > 0
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


def test_pick_port_avoids_busy_port():
    busy = _free_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", busy))
        s.listen(1)
        assert launcher.pick_port(avoid=busy) != busy


def test_is_our_instance_false_when_nothing_listening():
    assert launcher.is_our_instance(_free_port()) is False


def test_is_our_instance_true_for_live_server():
    port = _free_port()
    httpd = launcher.make_probe_server(port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert launcher.is_our_instance(port) is True
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_wait_until_serving_returns_promptly_when_up():
    port = _free_port()
    httpd = launcher.make_probe_server(port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        start = time.perf_counter()
        assert launcher.wait_until_serving(port, timeout=5) is True
        assert time.perf_counter() - start < 4
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_wait_until_serving_times_out_when_down():
    assert launcher.wait_until_serving(_free_port(), timeout=0.5) is False


def test_open_browser_is_deferred_until_server_is_listening(monkeypatch):
    """核心回归：浏览器必须在 server 真的 listening 之后才打开。

    提前调用 webbrowser.open 是这类工具最常见的差评来源——用户看到的是
    ERR_CONNECTION_REFUSED。此测试锁死顺序。

    注意：不要 monkeypatch 整个 threading.Thread，那是全局模块，
    会连带影响 pytest 自己的线程并挂起整个测试进程。
    """
    events = []
    monkeypatch.setattr(launcher.webbrowser, "open",
                        lambda url: events.append(("browser", url)))

    port = _free_port()
    httpd = launcher.make_probe_server(port)
    # server 在 open_browser 触发之后才开始 serve：
    # 能打开浏览器本身就证明端口那时已经可连。
    launcher.open_browser_after_ready(
        port, ready=lambda: threading.Thread(
            target=httpd.serve_forever, daemon=True).start())

    deadline = time.time() + 5
    while not events and time.time() < deadline:
        time.sleep(0.05)
    try:
        assert events, "浏览器没被打开"
        assert launcher.is_our_instance(port) is True
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_record_roundtrip(tmp_path):
    """记录的端口要能被下次启动读出来，认出自己"""
    rec = str(tmp_path / "instance.json")
    launcher.write_record(12345, rec)
    with open(rec, encoding="utf-8") as f:
        assert '"port"' in f.read()


def test_find_existing_instance_ignores_stale_record(tmp_path):
    """上次崩溃留下的记录指向一个没人监听的端口时，必须当作没有实例"""
    rec = str(tmp_path / "instance.json")
    launcher.write_record(_free_port(), rec)
    assert launcher.find_existing_instance(rec) is None


def test_find_existing_instance_detects_live_instance(tmp_path):
    rec = str(tmp_path / "instance.json")
    port = _free_port()
    httpd = launcher.make_probe_server(port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    launcher.write_record(port, rec)
    try:
        assert launcher.find_existing_instance(rec) == port
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_clear_record_is_idempotent(tmp_path):
    rec = str(tmp_path / "instance.json")
    launcher.write_record(1, rec)
    launcher.clear_record(rec)
    launcher.clear_record(rec)      # 不该抛异常
    assert launcher.find_existing_instance(rec) is None
