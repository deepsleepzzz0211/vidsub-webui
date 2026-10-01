"""退出时不能留下孤儿进程

Windows 上以 detached 方式起的子进程不会随父进程退出而消失，会残留成孤儿、
白占几 GB 内存。shell 里后台起的服务则相反——会随脚本退出被回收。
两种行为都要处理，所以这里直接测"登记的 pid 在清理后确实没了"。
"""
import subprocess
import sys
import time

from vidsub import server as srv


def _pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                         capture_output=True, text=True, errors="replace").stdout
    return str(pid) in out and "no tasks" not in out.lower()


def _spawn_sleeper():
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        creationflags=0x00000008)      # DETACHED_PROCESS，模拟推理服务


def test_terminate_children_kills_registered_pids():
    proc = _spawn_sleeper()
    try:
        srv.register_child(proc.pid)
        srv._terminate_children()
        deadline = time.time() + 10
        while _pid_alive(proc.pid) and time.time() < deadline:
            time.sleep(0.2)
        assert not _pid_alive(proc.pid), "登记的子进程没被清理"
    finally:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)],
                           capture_output=True)
        except Exception:
            pass


def test_terminate_children_clears_registry():
    """清理后登记表要清空，否则第二次清理会重复杀已死进程"""
    proc = _spawn_sleeper()
    srv.register_child(proc.pid)
    srv._terminate_children()
    with srv._CHILDREN_LOCK:
        assert srv._CHILDREN == []
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)],
                       capture_output=True)
    except Exception:
        pass


def test_terminate_children_survives_bad_pid():
    """已经死掉的 pid 不该让清理整体崩掉（否则会跳过剩下的）"""
    dead = _spawn_sleeper()
    dead.kill()
    dead.wait(timeout=10)
    alive = _spawn_sleeper()
    try:
        srv.register_child(dead.pid)
        srv.register_child(alive.pid)
        srv._terminate_children()
        deadline = time.time() + 10
        while _pid_alive(alive.pid) and time.time() < deadline:
            time.sleep(0.2)
        assert not _pid_alive(alive.pid)
    finally:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(alive.pid)],
                           capture_output=True)
        except Exception:
            pass
