"""下载页的服务端接口与队列

关键行为：
- 首次进入要能看到四个权重及体积（哪怕一个都还没下）
- 缺模型时要能被前端发现并引导去下载页
- 进度可轮询；下载失败要能看到原因
- 队列必须串行（CPU 与带宽独占，并发只会更慢）
- 下载完成后协议全文要随权重落盘（网易协议 3.4b）

真实权重合计 2.9GB，测试里不能真下。这里用「假权重 + 替换哈希计算」
的方式走通完整判定链：体积对 + 哈希过 → ready。

注意：后台下载是**线程**，测试必须等它结束（wait_idle）再做断言，
且每个测试用独立的数据目录与哈希表，否则线程在 teardown 之后还在写，
会污染下一个测试。
"""
import os
import sys
import time

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "tools"))

from vidsub import downloader  # noqa: E402
from vidsub.downloads import (STATE_DONE, STATE_ERROR, STATE_IDLE,  # noqa: E402
                              DownloadManager)


class Fakes:
    """单个测试用的假权重集合

    **关键：把每个权重的体积缩小到 64 KB。** 真实清单里四个权重合计
    2.9GB；测试若照搬真实 size_bytes，每跑一次就在临时目录堆 2.9GB。
    实测把 C 盘临时目录撑到 47GB、磁盘写满，测试随后以
    "No space left on device" 随机失败 —— 表现成毫无规律的 flaky，
    极易误判为并发问题。试过稀疏文件，在 NTFS 上并不可靠（仍占满空间）。

    下载器只关心「声明的体积」与「落盘体积」是否一致、以及哈希是否匹配，
    与体积的绝对值无关，因此缩小体积不影响被测逻辑。
    """

    SIZE = 64 * 1024

    def __init__(self, root: str):
        self.root = root
        self.hashes: dict[str, str] = {}
        self._patch_all_specs()

    def _patch_all_specs(self) -> None:
        """让 all_specs 返回缩小体积的 spec（用完必须还原）"""
        real = downloader.all_specs

        def _small(root: str):
            return [downloader.AssetSpec(
                key=s.key, label=s.label, url=s.url,
                size_bytes=self.SIZE, sha256=s.sha256, dest=s.dest,
                license_note=s.license_note, purpose=s.purpose)
                for s in real(root)]

        downloader.all_specs = _small
        self._real = real

    def restore(self) -> None:
        downloader.all_specs = self._real

    def write(self, spec) -> None:
        os.makedirs(os.path.dirname(spec.dest), exist_ok=True)
        with open(spec.dest, "wb") as f:
            f.write(b"y" * self.SIZE)      # 体积必须与声明一致
        self.hashes[os.path.abspath(spec.dest)] = spec.sha256

    def write_all(self) -> None:
        for s in downloader.all_specs(self.root):
            self.write(s)

    def sha256(self, path: str) -> str:
        return self.hashes.get(os.path.abspath(path), "")


@pytest.fixture
def fakes(tmp_path):
    root = str(tmp_path)
    obj = Fakes(root)
    real_hash = downloader.sha256_file
    downloader.sha256_file = lambda p: obj.sha256(p) or real_hash(p)
    try:
        yield obj
    finally:
        # 必须还原：all_specs 与 sha256_file 都是模块级，
        # 泄漏到别的测试会让它们的判定失真
        obj.restore()
        downloader.sha256_file = real_hash


@pytest.fixture
def fake_download(monkeypatch, fakes):
    """替换真实下载：保留真实的跳过判断，只把传输换掉"""

    def _dl(spec, progress=None, retries=3):
        if downloader.status_of(spec).state == "ready":
            return downloader.Result(ok=True, skipped=True)
        if progress:
            progress(spec.size_bytes // 2, spec.size_bytes)
        fakes.write(spec)
        if progress:
            progress(spec.size_bytes, spec.size_bytes)
        return downloader.Result(ok=True)
    monkeypatch.setattr(downloader, "download_one", _dl)
    return _dl


# --- 初始状态 -----------------------------------------------------------

def test_initial_snapshot_lists_all_assets(fakes):
    snap = DownloadManager(fakes.root).snapshot()
    assert snap["state"] == STATE_IDLE
    assert len(snap["items"]) == 4
    assert all(i["label"] for i in snap["items"])
    assert snap["total_bytes"] == 4 * Fakes.SIZE
    assert all(i["state"] == "missing" for i in snap["items"])


def test_snapshot_reports_partial_progress(fakes):
    s = downloader.all_specs(fakes.root)[0]
    half = s.size_bytes // 2
    os.makedirs(os.path.dirname(s.dest), exist_ok=True)
    with open(s.dest, "wb") as f:
        f.write(b"y" * half)
    downloader._write_sidecar(s, half)   # 边车背书这段是我们写的

    snap = DownloadManager(fakes.root).snapshot()
    item = next(i for i in snap["items"] if i["key"] == "asr_model")
    assert item["state"] == "partial"
    assert 0 < snap["progress"] < 1


def test_all_ready_when_everything_downloaded(fakes):
    fakes.write_all()
    m = DownloadManager(fakes.root)
    assert m.all_ready() is True
    assert m.missing() == []
    assert m.snapshot()["progress"] == 1.0


def test_missing_lists_only_absent(fakes):
    fakes.write_all()
    specs = downloader.all_specs(fakes.root)
    os.remove(specs[1].dest)
    got = DownloadManager(fakes.root).missing()
    assert len(got) == 1 and got[0]["key"] == specs[1].key


def test_license_note_exposed_to_frontend(fakes):
    """R2T2 是自定义协议，页面上要能看到提醒"""
    snap = DownloadManager(fakes.root).snapshot()
    r2t2 = next(i for i in snap["items"] if i["key"] == "asr_model")
    assert "NetEase" in r2t2["note"] or "网易" in r2t2["note"]


# --- 下载流程 -----------------------------------------------------------

def test_download_completes_and_marks_ready(fakes, fake_download):
    m = DownloadManager(fakes.root)
    m.start()
    assert m.wait_idle(timeout=30), "下载任务没在超时内结束"
    snap = m.snapshot()
    assert snap["state"] == STATE_DONE
    assert all(i["state"] == "ready" for i in snap["items"])
    assert m.all_ready()


def test_download_failure_surfaces_reason(fakes, monkeypatch):
    def _boom(spec, progress=None, retries=3):
        return downloader.Result(ok=False, error="校验失败：期望 abc 实际 def")
    monkeypatch.setattr(downloader, "download_one", _boom)

    m = DownloadManager(fakes.root)
    m.start()
    assert m.wait_idle(timeout=30)
    snap = m.snapshot()
    assert snap["state"] == STATE_ERROR
    assert "校验失败" in snap["error"]
    failed = [i for i in snap["items"] if i["state"] == "failed"]
    assert failed and "校验失败" in failed[0]["note"]


def test_partial_failure_still_reports_error_not_done(fakes, monkeypatch):
    """中途失败时最终状态必须是 error，不能被 worker 覆盖成 done。

    早期版本里 worker 无条件写 done，把 _run_task 设的 error 盖掉了 ——
    页面上会显示"完成"，但其实有文件没下好。
    """
    seen = {"n": 0}

    def _fail_second(spec, progress=None, retries=3):
        seen["n"] += 1
        if seen["n"] == 2:
            return downloader.Result(ok=False, error="网络中断")
        fakes.write(spec)
        return downloader.Result(ok=True)

    monkeypatch.setattr(downloader, "download_one", _fail_second)
    m = DownloadManager(fakes.root)
    m.start()
    assert m.wait_idle(timeout=30)
    assert m.snapshot()["state"] == STATE_ERROR
    assert "网络中断" in m.snapshot()["error"]


def test_license_files_copied_after_download(fakes, fake_download):
    """协议全文要随权重一起落盘（3.4b 条）"""
    m = DownloadManager(fakes.root)
    m.start()
    assert m.wait_idle(timeout=30)
    lic = os.path.join(fakes.root, "licenses", "MODEL_LICENSE-R2T2.md")
    assert os.path.exists(lic), "协议全文没落盘"
    assert os.path.getsize(lic) > 1000


def test_on_change_callback_fires(fakes, fake_download):
    hits = []
    m = DownloadManager(fakes.root, on_change=lambda: hits.append(1))
    m.start()
    assert m.wait_idle(timeout=30)
    assert len(hits) >= 8, "每个权重至少应触发两次通知"


def test_queue_is_serial(fakes, monkeypatch):
    """一个管理器内多任务必须串行 —— CPU 与带宽独占，并发只会更慢"""
    cur = {"now": 0, "max": 0}

    def _dl(spec, progress=None, retries=3):
        if downloader.status_of(spec).state == "ready":
            return downloader.Result(ok=True, skipped=True)
        cur["now"] += 1
        cur["max"] = max(cur["max"], cur["now"])
        time.sleep(0.05)
        fakes.write(spec)
        cur["now"] -= 1
        return downloader.Result(ok=True)

    monkeypatch.setattr(downloader, "download_one", _dl)
    m = DownloadManager(fakes.root)
    m.start()
    m.start()          # 排第二个任务
    m.start()          # 排第三个
    assert m.wait_idle(timeout=60)
    assert cur["max"] == 1, f"并发度 {cur['max']}，应为 1"


def test_second_start_while_running_does_not_duplicate(fakes, monkeypatch):
    """重复点"开始"不应把已下好的权重再下一遍"""
    calls = {"n": 0}

    def _faithful(spec, progress=None, retries=3):
        if downloader.status_of(spec).state == "ready":
            return downloader.Result(ok=True, skipped=True)
        calls["n"] += 1
        time.sleep(0.05)
        fakes.write(spec)
        return downloader.Result(ok=True)

    monkeypatch.setattr(downloader, "download_one", _faithful)
    m = DownloadManager(fakes.root)
    m.start()
    time.sleep(0.02)
    m.start()
    assert m.wait_idle(timeout=30)
    # 第一个任务下完 4 个；第二个任务发现自己已就绪，不再重复下载
    assert calls["n"] == 4, f"应恰好下载 4 次，实际 {calls['n']}"
