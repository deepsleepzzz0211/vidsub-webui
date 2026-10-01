"""发现与收编的服务端接口

要覆盖的用户可见行为：
- 页面能知道"我们找过哪些地方"（不然用户无从判断该不该自己去下）
- 找得到就收编，找不到给出**去魔搭下载**的单文件可续传指引
- 用户手动指目录也能认领
- 认领要按 sha256 校验，认错了不能蒙混过关
"""
import hashlib
import os

import pytest
from fastapi.testclient import TestClient

from vidsub import downloader, registry, server


SIZE = 2048


def _fake_bytes(key="x") -> bytes:
    """每个权重用**不同内容**，体积精确等于 SIZE。

    两个坑都要避开：
    - 体积必须精确：认领是"先按体积筛、再按哈希验"，写短了会被体积筛挡掉，
      症状是"目录里明明有那个文件，却说认不出来"。
    - 内容必须各不相同：四个权重内容一样的话，它们哈希相同，扫描会全部
      认成同一个资产，症状是"只认领了一个，其余说没找到"。
    """
    seed = (key + "-weight-bytes-").encode()
    out = (seed * (SIZE // len(seed) + 1))[:SIZE]
    assert len(out) == SIZE
    return out


def _make(path, key="x", body=b""):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(body or _fake_bytes(key))
    return str(path)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDSUB_DATA_DIR", str(tmp_path / "data"))
    with TestClient(server.app) as c:
        yield c


def _shrink(monkeypatch, size=SIZE):
    """把四个权重的体积缩到 2 KB，测试里不必造 2.6 GB"""
    real = registry.ASSETS
    small = []
    for a in real:
        small.append(registry.Asset(
            key=a.key, label=a.label, repo=a.repo, path=a.path,
            size_bytes=size,
            sha256=hashlib.sha256(_fake_bytes(a.key)).hexdigest(),
            hf_repo=a.hf_repo, hf_path=a.hf_path, source=a.source,
            license_note=a.license_note, purpose=a.purpose))
    monkeypatch.setattr(registry, "ASSETS", tuple(small))
    return small


# --- 现状报告 -----------------------------------------------------------

def test_models_reports_cache_roots(client):
    """页面要能看到"找过哪些地方" """
    b = client.get("/api/models").json()
    kinds = {r["kind"] for r in b["caches"]["roots"]}
    assert kinds == {"huggingface", "modelscope"}
    for r in b["caches"]["roots"]:
        assert r["path"], "每个缓存根都要给出路径"
        assert "exists" in r


def test_models_lists_four_assets_and_size(client):
    b = client.get("/api/models").json()
    assert len(b["items"]) == 4
    assert b["ready"] is False
    assert b["total_bytes"] > 2 * 1024 ** 3


def test_env_overrides_are_reported(client, monkeypatch, tmp_path):
    """用户自己指定了缓存位置，要如实说是哪个环境变量定的"""
    monkeypatch.setenv("MODELSCOPE_CACHE", str(tmp_path / "mycache"))
    b = client.get("/api/models").json()
    ms = next(r for r in b["caches"]["roots"] if r["kind"] == "modelscope")
    assert ms["env_var"] == "MODELSCOPE_CACHE"
    assert ms["path"] == str(tmp_path / "mycache")


def test_default_location_is_not_attributed_to_an_env_var(client, monkeypatch):
    """没设环境变量时别谎称是它决定的 —— 会让人去改一个没生效的变量"""
    for v in ("HF_HOME", "HF_HUB_CACHE", "MODELSCOPE_CACHE"):
        monkeypatch.delenv(v, raising=False)
    b = client.get("/api/models").json()
    assert all(r["env_var"] == "" for r in b["caches"]["roots"])


# --- 去魔搭下载的指引 ---------------------------------------------------

def test_hints_cover_every_missing_asset(client):
    """四个都没下时，四个都要有指引"""
    b = client.get("/api/models").json()
    assert len(b["hints"]) == 4
    assert {h["key"] for h in b["hints"]} == {a.key for a in registry.ASSETS}


def test_hint_points_at_modelscope(client):
    h = client.get("/api/models").json()["hints"][0]
    assert h["repo"] and "/" in h["repo"]
    assert h["url"].startswith("https://")


def test_hint_is_resumable_and_single_file(client):
    """指引必须能续传、且只下单个文件"""
    for h in client.get("/api/models").json()["hints"]:
        assert "-C -" in h["curl"], f"{h['key']} 的命令不能续传"
        assert os.path.basename(h["path"]) in h["curl"]


def test_hint_sheds_size_in_readable_units(client):
    """1056 MB 不该显示成 '1.0 GB' —— 像占位符写错了"""
    h = next(x for x in client.get("/api/models").json()["hints"]
             if x["key"] == "asr_model")
    assert h["size_text"].endswith("MB")


def test_vad_hint_uses_its_actual_host(client):
    """VAD 在 GitHub 上（ModelScope 上没有），指引要指向真实可下的地址"""
    h = next(x for x in client.get("/api/models").json()["hints"]
             if x["key"] == "vad")
    assert "silero_vad.onnx" in h["curl"]
    assert "githubusercontent" in h["url"] or "github" in h["url"]


def test_no_hints_once_everything_ready(client, monkeypatch, tmp_path):
    small = _shrink(monkeypatch)
    for a in small:
        spec = downloader.spec_for(a, server.data_dir())
        os.makedirs(os.path.dirname(spec.dest), exist_ok=True)
        with open(spec.dest, "wb") as f:
            f.write(_fake_bytes(a.key))
    b = client.get("/api/models").json()
    assert b["ready"] is True
    assert b["hints"] == []


# --- 从标准缓存收编 -----------------------------------------------------

def test_rescan_adopts_from_modelscope_cache(client, monkeypatch, tmp_path):
    """权重已经在魔搭缓存里 → 重扫就能收编，不用下载"""
    small = _shrink(monkeypatch)
    cache = tmp_path / "mscache"
    monkeypatch.setenv("MODELSCOPE_CACHE", str(cache))
    for a in small:
        p = cache / a.repo / a.path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_fake_bytes(a.key))

    r = client.post("/api/models/rescan")
    assert r.status_code == 200
    b = r.json()
    assert b["ready"] is True
    assert all(x["ok"] for x in b["results"] if x["key"] in {a.key for a in small})
    assert all(x["source"] == "modelscope" for x in b["results"])


def test_rescan_reports_not_found_instead_of_failing(client):
    """缓存里没有是正常情况，要如实说，不要报错"""
    b = client.post("/api/models/rescan").json()
    assert b["ready"] is False
    assert b["results"]
    assert any("没找到" in x["error"] for x in b["results"])


def test_rescan_rejects_wrong_content(client, monkeypatch, tmp_path):
    """缓存里同名但内容不对的文件不能被认领 —— 免得拿到跑不起来的权重"""
    small = _shrink(monkeypatch)
    cache = tmp_path / "mscache"
    monkeypatch.setenv("MODELSCOPE_CACHE", str(cache))
    a = small[0]
    p = cache / a.repo / a.path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * 2048)          # 体积对，内容是垃圾

    b = client.post("/api/models/rescan").json()
    assert b["ready"] is False


# --- 用户手动指目录 -----------------------------------------------------

def test_adopt_manual_directory(client, monkeypatch, tmp_path):
    small = _shrink(monkeypatch)
    lib = tmp_path / "mylib"
    for a in small:
        # 文件名与层级都跟清单不一致 —— 只认 sha256
        _make(lib / "random-sub" / f"whatever-{a.key}.bin", a.key)

    r = client.post("/api/models/adopt", json={"path": str(lib)})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["ready"] is True
    assert len(b["results"]) == 4


def test_adopt_requires_a_path(client):
    r = client.post("/api/models/adopt", json={})
    assert r.status_code == 400
    assert "path" in r.json()["error"]


def test_adopt_rejects_missing_directory(client, tmp_path):
    r = client.post("/api/models/adopt", json={"path": str(tmp_path / "nope")})
    assert r.status_code == 400
    assert "不存在" in r.json()["error"]


def test_adopt_explains_when_nothing_matches(client, tmp_path):
    lib = tmp_path / "junk"
    _make(lib / "notes.txt", body=b"just notes")
    r = client.post("/api/models/adopt", json={"path": str(lib)})
    assert r.status_code == 404
    assert "sha256" in r.json()["error"]


def test_adopt_does_not_delete_source(client, monkeypatch, tmp_path):
    """收编不能删用户的源文件 —— 那可能是人家唯一的副本"""
    small = _shrink(monkeypatch)
    lib = tmp_path / "mylib"
    srcs = [_make(lib / f"{a.key}.bin", a.key) for a in small]

    client.post("/api/models/adopt", json={"path": str(lib)})
    for p in srcs:
        assert os.path.exists(p), f"源文件被删了：{p}"


def test_adopt_copies_license_alongside(client, monkeypatch, tmp_path):
    """收编也要把协议全文放好（网易协议 3.4b 要求每个副本保留）"""
    small = _shrink(monkeypatch)
    lib = tmp_path / "mylib"
    for a in small:
        _make(lib / f"{a.key}.bin", a.key)

    client.post("/api/models/adopt", json={"path": str(lib)})
    lic = os.path.join(server.data_dir(), "licenses", "MODEL_LICENSE-R2T2.md")
    assert os.path.exists(lic), "协议全文没落盘"