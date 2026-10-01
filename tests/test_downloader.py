"""下载器：断点续传 + 校验 + 进度

要覆盖的行为（每条都对应一个真实需求）：
- 断点续传：1.1GB 下到一半断了，重来时要接着下，不是从头下
- 校验：sha256 不符一律视为未完成，且**删掉**半成品
- 跳过：已完成且校验通过的文件不再下载
- 进度：能报出已下字节数与总量

用本地 HTTP 服务模拟，不打真实网络。
"""
import hashlib
import http.server
import os
import threading

import pytest

from vidsub import downloader, registry


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


PAYLOAD = bytes(range(256)) * 400          # 102,400 字节，确定性内容


class _Handler(http.server.BaseHTTPRequestHandler):
    payload = PAYLOAD
    served = 0

    def do_GET(self):
        # 支持 Range，用于验证续传
        rng = self.headers.get("Range")
        data = self.payload
        if rng and rng.startswith("bytes="):
            start = int(rng.split("=")[1].split("-")[0])
            data = data[start:]
            self.send_response(206)
            self.send_header("Content-Range",
                             f"bytes {start}-{len(self.payload)-1}/{len(self.payload)}")
        else:
            self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        type(self).served += 1

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    _Handler.served = 0
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/model.gguf", _Handler
    srv.shutdown()
    srv.server_close()


def _asset(url: str, tmp, **kw):
    return downloader.AssetSpec(
        key="test", label="测试权重", url=url, size_bytes=len(PAYLOAD),
        sha256=_sha(PAYLOAD), dest=str(tmp / "model.gguf"), **kw)


# --- 校验 ---------------------------------------------------------------

def test_downloads_and_verifies(server, tmp_path):
    url, _ = server
    out = _asset(url, tmp_path)
    result = downloader.download_one(out, progress=None)
    assert result.ok
    assert os.path.getsize(out.dest) == len(PAYLOAD)
    assert downloader.sha256_file(out.dest) == _sha(PAYLOAD)


def test_checksum_mismatch_fails_and_removes_partial(server, tmp_path):
    """服务端给的内容与锁定哈希不符 → 报错，且**删掉**半成品与边车。

    留着它的话，下次会拿它续传，拼出更大的坏文件且永远校验不过。
    """
    url, _ = server
    spec = downloader.AssetSpec(
        key="t", label="t", url=url, size_bytes=len(PAYLOAD),
        sha256="0" * 64, dest=str(tmp_path / "m.gguf"))
    result = downloader.download_one(spec, progress=None, retries=1)
    assert not result.ok
    assert "sha256" in result.error.lower() or "校验" in result.error
    assert not os.path.exists(spec.dest)
    assert not os.path.exists(spec.dest + ".part")
    assert not os.path.exists(spec.dest + ".partmeta")


def test_size_mismatch_reports_clearly(server, tmp_path):
    """体积对不上要给出具体数字，便于判断是截断还是清单写错"""
    url, _ = server
    spec = downloader.AssetSpec(
        key="t", label="t", url=url, size_bytes=len(PAYLOAD) + 999,
        sha256=_sha(PAYLOAD), dest=str(tmp_path / "m.gguf"), )
    result = downloader.download_one(spec, progress=None, retries=1)
    assert not result.ok
    assert "体积" in result.error or "字节" in result.error


# --- 跳过 ---------------------------------------------------------------

def test_skips_when_already_complete_and_valid(server, tmp_path):
    url, _ = server
    spec = _asset(url, tmp_path)
    open(spec.dest, "wb").write(PAYLOAD)          # 假装已下好
    before = _Handler.served
    result = downloader.download_one(spec, progress=None)
    assert result.ok
    assert result.skipped, "已完整且校验通过时必须跳过"
    assert _Handler.served == before, "跳过时不该发任何请求"


def test_redownloads_when_checksum_wrong(server, tmp_path):
    """文件在但校验不过 → 重下，而不是当成已完成"""
    url, _ = server
    spec = _asset(url, tmp_path)
    open(spec.dest, "wb").write(b"x" * len(PAYLOAD))   # 体积对、内容错
    result = downloader.download_one(spec, progress=None)
    assert result.ok and not result.skipped
    assert downloader.sha256_file(spec.dest) == _sha(PAYLOAD)


# --- 断点续传 -----------------------------------------------------------

def test_resumes_from_partial(server, tmp_path):
    """核心需求：已有半成品时用 Range 续传，而不是从头下"""
    url, _ = server
    spec = _asset(url, tmp_path)
    cut = 40_000
    open(spec.dest, "wb").write(PAYLOAD[:cut])    # 模拟下到一半
    downloader._write_sidecar(spec, cut)          # 边车：我们自己写的这段可信

    before = _Handler.served
    result = downloader.download_one(spec, progress=None)
    assert result.ok
    assert result.resumed, "应走续传路径"
    assert downloader.sha256_file(spec.dest) == _sha(PAYLOAD)
    # 续传只该发一次请求（拿剩下的部分）
    assert _Handler.served - before == 1
    # 边车在成功后必须清掉，否则下次会把完整文件误当半成品
    assert not os.path.exists(spec.dest + ".partmeta")


def test_restarts_when_partial_has_no_sidecar(server, tmp_path):
    """没有边车背书的半成品不可信 → 整个重下（宁可多下，不可拿到坏文件）"""
    url, _ = server
    spec = _asset(url, tmp_path)
    open(spec.dest, "wb").write(b"z" * 40_000)    # 体积像半成品，内容是垃圾

    result = downloader.download_one(spec, progress=None)
    assert result.ok
    assert not result.resumed
    assert downloader.sha256_file(spec.dest) == _sha(PAYLOAD)


def test_resume_ignored_when_server_does_not_support_range(tmp_path):
    """服务端不支持 Range 时要能退回整段下载，而不是一直失败"""
    class NoRange(_Handler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.payload)))
            self.end_headers()
            self.wfile.write(self.payload)
            type(self).served += 1

    srv = http.server.HTTPServer(("127.0.0.1", 0), NoRange)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}/m.bin"
        spec = _asset(url, tmp_path)
        open(spec.dest, "wb").write(PAYLOAD[:30_000])
        downloader._write_sidecar(spec, 30_000)
        result = downloader.download_one(spec, progress=None)
        assert result.ok
        # 关键：服务端忽略 Range 时必须整个重下，否则会拼出内容错乱的文件
        assert downloader.sha256_file(spec.dest) == _sha(PAYLOAD)
    finally:
        srv.shutdown()
        srv.server_close()


# --- 进度 ---------------------------------------------------------------

def test_progress_callback_reports_monotonic_bytes(server, tmp_path):
    url, _ = server
    spec = _asset(url, tmp_path)
    seen: list[tuple[int, int]] = []

    def on_progress(done: int, total: int) -> None:
        seen.append((done, total))

    downloader.download_one(spec, progress=on_progress)
    assert seen, "应当报告进度"
    assert all(t == len(PAYLOAD) for _, t in seen), "总量应恒定"
    assert seen == sorted(seen), "进度不应回退"
    assert seen[-1][0] == len(PAYLOAD), "最终进度应等于总量"


def test_progress_reports_resume_baseline(server, tmp_path):
    """续传时首次上报的已下载量应包含已有的部分，否则进度条会从 0 开始闪"""
    url, _ = server
    spec = _asset(url, tmp_path)
    cut = 50_000
    open(spec.dest, "wb").write(PAYLOAD[:cut])
    downloader._write_sidecar(spec, cut)
    seen: list[int] = []
    downloader.download_one(spec, progress=lambda d, t: seen.append(d))
    assert seen and seen[0] >= cut, f"续传首报 {seen[0]} 应 ≥ 已有 {cut}"


# --- 状态查询（给下载页用）----------------------------------------------

def test_status_reflects_reality(server, tmp_path):
    url, _ = server
    spec = _asset(url, tmp_path)
    assert downloader.status_of(spec).state == "missing"

    open(spec.dest, "wb").write(PAYLOAD)
    assert downloader.status_of(spec).state == "ready"

    # 体积够但内容错 → corrupt
    open(spec.dest, "wb").write(b"x" * len(PAYLOAD))
    assert downloader.status_of(spec).state == "corrupt"

    # 体积不足且无边车背书 → 也算 corrupt，不当作可续传的半成品
    open(spec.dest, "wb").write(b"y" * 1000)
    assert downloader.status_of(spec).state == "corrupt"


def test_short_file_with_sidecar_is_partial(tmp_path):
    """有边车背书、长度对得上，才算真半成品"""
    spec = _asset("http://127.0.0.1:1/x", tmp_path)
    open(spec.dest, "wb").write(PAYLOAD[:5000])
    downloader._write_sidecar(spec, 5000)
    st = downloader.status_of(spec)
    assert st.state == "partial"
    assert st.have_bytes == 5000
    assert st.total_bytes == len(PAYLOAD)


def test_sidecar_with_stale_length_is_corrupt(tmp_path):
    """边车长度与实际文件不符 → 不可信，当 corrupt"""
    spec = _asset("http://127.0.0.1:1/x", tmp_path)
    open(spec.dest, "wb").write(PAYLOAD[:5000])
    downloader._write_sidecar(spec, 9999)      # 谎报
    assert downloader.status_of(spec).state == "corrupt"


def test_sidecar_with_other_expected_hash_is_ignored(tmp_path):
    """换了权重（期望哈希变了）→ 旧边车不可信"""
    spec = _asset("http://127.0.0.1:1/x", tmp_path)
    open(spec.dest, "wb").write(PAYLOAD[:5000])
    downloader._write_sidecar(spec, 5000)
    other = downloader.AssetSpec(**{**spec.__dict__, "sha256": "a" * 64})
    assert downloader.status_of(other).state == "corrupt"


def test_all_statuses_for_registry(tmp_path):
    """清单里每个权重都能给出状态（下载页要用）"""
    for a in registry.ASSETS:
        spec = downloader.spec_for(a, tmp_path)
        st = downloader.status_of(spec)
        assert st.state == "missing"
        assert st.total_bytes == a.size_bytes


# --- 416：下满了却被判失败（真实踩过）------------------------------------

def test_complete_part_file_does_not_request_range_again(server, tmp_path):
    """体积已够的 .part 不该再发 Range 请求。

    实测踩过：1.1GB 下满后，重试逻辑又发了一次 bytes=<size>-，
    服务端回 416（Requested Range Not Satisfiable），最终报"下载失败" ——
    其实文件早已完整。表现为"明明下满了却报错"，很容易误判成网络问题。
    """
    url, _ = server
    spec = _asset(url, tmp_path)
    part = spec.dest + ".part"
    with open(part, "wb") as f:
        f.write(PAYLOAD)                      # 体积已满
    downloader._write_sidecar(spec, len(PAYLOAD))

    before = _Handler.served
    result = downloader.download_one(spec, progress=None)
    assert result.ok, f"已完整的 .part 应直接进校验并就位，实得：{result.error}"
    assert os.path.exists(spec.dest)
    assert downloader.sha256_file(spec.dest) == _sha(PAYLOAD)
    assert _Handler.served == before, "不该再发任何请求"


def test_complete_part_with_bad_hash_still_fails(server, tmp_path):
    """体积够但内容坏时仍要报校验失败（416 的修复不能绕过校验）"""
    url, _ = server
    spec = _asset(url, tmp_path)
    part = spec.dest + ".part"
    with open(part, "wb") as f:
        f.write(b"z" * len(PAYLOAD))
    downloader._write_sidecar(spec, len(PAYLOAD))

    result = downloader.download_one(spec, progress=None, retries=1)
    assert not result.ok
    assert "sha256" in result.error.lower() or "校验" in result.error
    assert not os.path.exists(part), "校验失败后应删掉半成品"
