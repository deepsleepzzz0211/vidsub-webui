"""按域名选代理

实测（本机，同一 URL 对比）：

| 源 | 直连 | 走代理 |
|---|---|---|
| ModelScope | **35 MB/s** | 240 KB/s |
| GitHub raw（VAD） | 只拿到 268KB/2.3MB 就断 | 完整 |

也就是说代理**只对 GitHub 有用**，而且用它去跑 ModelScope 会慢 87 倍。
早先把它做成全局开关（设了 `VIDSUB_DOWNLOAD_PROXY` 就所有请求都走代理），
结果 2.6GB 的下载全被按住了 —— 而 2.6 GB 几乎全在 ModelScope 上。

正确做法：
- 默认直连
- 已知直连有问题的域名走代理
- 直连失败自动回退代理，并记住（别每次重试都再付一次超时）
- 用户在墙内可以把所有流量强制走代理
"""
import http.server
import threading

import pytest

from vidsub import downloader

MS_URL = ("https://www.modelscope.cn/api/v1/models/org/repo"
          "?Revision=master&FilePath=a.gguf")
GH_URL = ("https://raw.githubusercontent.com/snakers4/silero-vad/v5.1.2/"
          "src/silero_vad/data/silero_vad.onnx")
HF_URL = "https://huggingface.co/org/repo/resolve/main/a.gguf"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in (downloader.PROXY_ENV, downloader.PROXY_HOSTS_ENV,
              downloader.FORCE_DIRECT_ENV):
        monkeypatch.delenv(v, raising=False)
    downloader._reset_route_cache()
    yield
    downloader._reset_route_cache()


# --- 选路矩阵 -----------------------------------------------------------

def test_defaults_to_direct(clean_env):
    """没配代理时一律直连，且**不用系统代理**"""
    assert downloader._use_proxy(MS_URL) is False
    assert downloader._use_proxy(GH_URL) is False


def test_modelscope_stays_direct_even_with_proxy_configured(monkeypatch):
    """核心修复：配了代理也不能把 ModelScope 拖进代理

    实测走代理 240 KB/s vs 直连 35 MB/s。这条要是破了，2.6GB 的下载
    会从一分钟变成三小时。
    """
    monkeypatch.setenv(downloader.PROXY_ENV, "http://127.0.0.1:7897")
    downloader._reset_route_cache()
    assert downloader._use_proxy(MS_URL) is False


def test_github_uses_proxy_when_configured(monkeypatch):
    """GitHub 直连会截断，必须走代理"""
    monkeypatch.setenv(downloader.PROXY_ENV, "http://127.0.0.1:7897")
    downloader._reset_route_cache()
    assert downloader._use_proxy(GH_URL) is True


def test_github_without_proxy_is_still_direct(clean_env):
    """没配代理就别硬凑 —— 直连截断是环境问题，不该让代码假装能修"""
    assert downloader._use_proxy(GH_URL) is False


def test_force_direct_routes_everything_through_proxy(monkeypatch):
    """墙内用户要能强制全走代理"""
    monkeypatch.setenv(downloader.PROXY_ENV, "http://127.0.0.1:7897")
    monkeypatch.setenv(downloader.FORCE_DIRECT_ENV, "0")
    downloader._reset_route_cache()
    assert downloader._use_proxy(MS_URL) is True
    assert downloader._use_proxy(GH_URL) is True


def test_custom_proxy_hosts_can_be_added(monkeypatch):
    """内网镜像 / 自建源也要能加进来"""
    monkeypatch.setenv(downloader.PROXY_ENV, "http://127.0.0.1:7897")
    monkeypatch.setenv(downloader.PROXY_HOSTS_ENV, "mirror.internal")
    downloader._reset_route_cache()
    assert downloader._use_proxy("https://mirror.internal/x/a.gguf") is True
    assert downloader._use_proxy(MS_URL) is False


def test_builtin_proxy_hosts_are_always_applied(monkeypatch):
    """自定义列表是**追加**，不该把内置的 GitHub 顶掉"""
    monkeypatch.setenv(downloader.PROXY_ENV, "http://127.0.0.1:7897")
    monkeypatch.setenv(downloader.PROXY_HOSTS_ENV, "mirror.internal")
    downloader._reset_route_cache()
    assert downloader._use_proxy(GH_URL) is True


def test_proxy_hosts_parse_tolerates_whitespace(monkeypatch):
    monkeypatch.setenv(downloader.PROXY_ENV, "http://127.0.0.1:7897")
    monkeypatch.setenv(downloader.PROXY_HOSTS_ENV, " a.com , b.com ,, ")
    downloader._reset_route_cache()
    assert downloader._use_proxy("https://a.com/x") is True
    assert downloader._use_proxy("https://b.com/x") is True
    assert downloader._use_proxy("https://c.com/x") is False


def test_hub_subdomain_of_builtin_matches(monkeypatch):
    """子域名也要算命中"""
    monkeypatch.setenv(downloader.PROXY_ENV, "http://127.0.0.1:7897")
    downloader._reset_route_cache()
    assert downloader._use_proxy("https://objects.githubusercontent.com/a") is True


# --- 直连失败回退代理 ----------------------------------------------------

def test_falls_back_to_proxy_when_direct_fails(monkeypatch):
    """直连挂了要能改走代理，而不是直接报错"""
    monkeypatch.setenv(downloader.PROXY_ENV, "http://127.0.0.1:7897")
    downloader._reset_route_cache()

    tried: list[bool] = []

    def _fake(url, start=0, use_proxy=False):
        tried.append(use_proxy)
        if not use_proxy:
            raise OSError("直连超时")
        return _ok_response(PAYLOAD)

    monkeypatch.setattr(downloader, "_raw_open", _fake)
    got = downloader._open(GH_URL)
    assert got is not None
    assert tried == [True], "已知代理域名不该先试直连"


def test_remembers_direct_failure_to_avoid_repeating_timeout(monkeypatch):
    """记住"这个域名直连不行"，别每次重试都再付一次超时

    真实体感：不记的话每个重试都要先卡满一次 connect timeout，
    3 次重试就是 3 倍等待。
    """
    monkeypatch.setenv(downloader.PROXY_ENV, "http://127.0.0.1:7897")
    downloader._reset_route_cache()

    tried: list[bool] = []

    def _fake(url, start=0, use_proxy=False):
        tried.append(use_proxy)
        if not use_proxy:
            raise OSError("直连超时")
        return _ok_response(PAYLOAD)

    monkeypatch.setattr(downloader, "_raw_open", _fake)
    downloader._open("https://mirror.example.com/a.bin")
    downloader._open("https://mirror.example.com/a.bin")
    assert tried == [False, True, True], f"直连只该试一次，实际 {tried}"


def test_no_fallback_without_proxy_configured(monkeypatch):
    """没配代理时没什么可回退，直接抛"""
    monkeypatch.setattr(downloader, "_raw_open",
                        lambda url, start=0, use_proxy=False: (_ for _ in ()).throw(
                            OSError("连不上")))
    with pytest.raises(OSError):
        downloader._open(MS_URL)


def test_fallback_does_not_mask_real_http_errors(monkeypatch):
    """404 之类不该被当成"直连不行"而重试代理 —— 换个地方也是 404"""
    import urllib.error

    monkeypatch.setenv(downloader.PROXY_ENV, "http://127.0.0.1:7897")
    downloader._reset_route_cache()

    calls: list[bool] = []

    def _fake(url, start=0, use_proxy=False):
        calls.append(use_proxy)
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    monkeypatch.setattr(downloader, "_raw_open", _fake)
    with pytest.raises(urllib.error.HTTPError):
        downloader._open(MS_URL)
    assert calls == [False], "HTTP 错误不该触发代理回退"


# --- 真实链路：代理决策要真的影响请求 -----------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    payload = b"x" * 1024

    def do_GET(self):
        rng = self.headers.get("Range")
        data = self.payload
        if rng and rng.startswith("bytes="):
            s = int(rng.split("=")[1].split("-")[0])
            data = data[s:]
            self.send_response(206)
            self.send_header("Content-Range",
                             f"bytes {s}-{len(self.payload)-1}/{len(self.payload)}")
        else:
            self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def _ok_response(body: bytes):
    """造一个最小的"响应对象"，只需支持上下文管理器与 read()"""
    class R:
        status = 200

        def read(self, n=-1):
            d = self._b[:n if n and n > 0 else len(self._b)]
            self._b = self._b[len(d):]
            return d

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    r = R()
    r._b = body
    return r


PAYLOAD = _Handler.payload


@pytest.fixture
def local_server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/a.bin"
    srv.shutdown()
    srv.server_close()


def test_real_download_over_direct_path(local_server, tmp_path):
    """端到端：本地源走直连也能下完"""
    import hashlib

    spec = downloader.AssetSpec(
        key="t", label="t", url=local_server, size_bytes=len(PAYLOAD),
        sha256=hashlib.sha256(PAYLOAD).hexdigest(),
        dest=str(tmp_path / "a.bin"))
    r = downloader.download_one(spec, progress=None)
    assert r.ok, r.error
    assert downloader.sha256_file(spec.dest) == spec.sha256


def test_forced_proxy_really_routes_through_the_proxy(local_server, tmp_path,
                                                      monkeypatch):
    """强制代理模式要真的把请求交给代理，而不是只在决策函数里返回 True

    起一个最小转发代理（http:// 走绝对 URI，不需要 CONNECT），
    验证请求确实从它那儿过 —— 否则这条路径永远没被跑过，真出问题时才发现。
    """
    import hashlib
    import http.client
    import urllib.parse as up

    seen: list[str] = []

    class Proxy(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            parts = up.urlsplit(self.path)
            conn = http.client.HTTPConnection(parts.hostname,
                                              parts.port or 80, timeout=10)
            tail = parts.path + (("?" + parts.query) if parts.query else "")
            conn.request("GET", tail)
            resp = conn.getresponse()
            body = resp.read()
            self.send_response(resp.status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    psrv = http.server.HTTPServer(("127.0.0.1", 0), Proxy)
    threading.Thread(target=psrv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv(downloader.PROXY_ENV,
                           f"http://127.0.0.1:{psrv.server_port}")
        monkeypatch.setenv(downloader.FORCE_DIRECT_ENV, "0")
        downloader._reset_route_cache()

        spec = downloader.AssetSpec(
            key="t", label="t", url=local_server, size_bytes=len(PAYLOAD),
            sha256=hashlib.sha256(PAYLOAD).hexdigest(),
            dest=str(tmp_path / "a.bin"))
        r = downloader.download_one(spec, progress=None, retries=1)
        assert r.ok, r.error
        assert seen, "强制代理模式下请求没经过代理 —— 选路逻辑没真正生效"
    finally:
        psrv.shutdown()
        psrv.server_close()