"""收编本地已有权重（adopt）

为什么要这个功能：
用户**不一定**从零开始。常见情形——
- 已经手工下过权重，装 vidsub 时不该再下一遍 2.6 GB
- 离线环境 / 内网分发，文件是别的同事拷来的
- 同一台机器上多个项目共用一份权重

实测踩过：明明 `r2t2-test/` 下四个权重齐全，却因为数据目录指向空的
临时目录，把 2.6 GB 从头下了一遍——而且 registry 里的哈希本来就是
从这些本地文件算出来的，等于把同一份数据又拉了一次。

因此：**先看本地有没有，校验过了就直接收编，不下载。**
"""
import hashlib
import os

from vidsub import downloader, registry


BODY = b"hello-vidsub"          # 11 字节
SIZE = len(BODY)
SHA = hashlib.sha256(BODY).hexdigest()


def _make(tmp_path, name: str, body: bytes = BODY) -> str:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return str(p)


def _spec(root, size=SIZE, sha=None, key="test"):
    return downloader.AssetSpec(
        key=key, label="测试", url="http://127.0.0.1:1/x",
        size_bytes=size, sha256=sha or SHA,
        dest=os.path.join(str(root), "sub", "model.bin"))


def _fake_asset(key="fake", body=BODY):
    """体积很小的假资产：真实权重 1.1GB，测试里没法造"""
    return registry.Asset(
        key=key, label=f"假权重 {key}", repo="o/r", path=f"{key}.bin",
        size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        hf_repo="o/r", hf_path=f"{key}.bin")


# --- 校验是前提 ---------------------------------------------------------

def test_adopt_verifies_hash_before_touching_anything(tmp_path):
    """哈希不符必须拒绝，且**不能**动目标位置的文件

    特意造"体积对得上、内容不对"的情况，因为这才是危险的那种 ——
    只比体积会放过它。
    """
    root = tmp_path / "data"
    spec = _spec(root, sha="0" * 64)          # 体积对，哈希是假的
    src = _make(tmp_path, "wrong.bin", BODY)

    res = downloader.adopt(spec, src)
    assert not res.ok
    assert "sha256" in res.error.lower() or "校验" in res.error
    assert not os.path.exists(spec.dest)


def test_adopt_rejects_size_mismatch(tmp_path):
    """体积对但哈希不对也算拒——只信哈希，不信体积"""
    root = tmp_path / "data"
    spec = _spec(root, size=999)
    src = _make(tmp_path, "a.bin")
    res = downloader.adopt(spec, src)
    assert not res.ok


def test_adopt_rejects_missing_source(tmp_path):
    res = downloader.adopt(_spec(tmp_path / "d"), str(tmp_path / "nope.bin"))
    assert not res.ok
    assert "不存在" in res.error or "no such" in res.error.lower()


def test_adopt_accepts_a_different_path(tmp_path):
    """文件名不必一致：只要内容对得上就该收编

    用户的文件可能叫 `Q4_K_M.gguf` 或躺在任意目录里，按路径对齐
    反而是自找麻烦。**按哈希认人才是稳的。**
    """
    root = tmp_path / "data"
    spec = _spec(root)
    src = _make(tmp_path, "some-random-name.gguf")

    res = downloader.adopt(spec, src)
    assert res.ok, res.error
    assert downloader.sha256_file(spec.dest) == spec.sha256
    assert os.path.exists(src), "源文件必须保留"


# --- 放置方式 -----------------------------------------------------------

def test_adopt_hardlinks_when_same_volume(tmp_path):
    """同盘用硬链接：不占额外空间，也不复制 1.1GB"""
    root = tmp_path / "data"
    spec = _spec(root)
    src = _make(tmp_path, "m.bin")

    res = downloader.adopt(spec, src)
    assert res.ok
    if res.linked:
        assert os.stat(spec.dest).st_ino == os.stat(src).st_ino


def test_adopt_falls_back_to_copy(tmp_path):
    """不支持硬链接时要退回复制，不能直接失败"""
    root = tmp_path / "data"
    spec = _spec(root)
    src = _make(tmp_path, "m.bin")

    orig = os.link
    os.link = lambda *a, **k: (_ for _ in ()).throw(OSError("no hardlink"))
    try:
        res = downloader.adopt(spec, src)
    finally:
        os.link = orig

    assert res.ok, res.error
    assert res.linked is False
    assert os.path.exists(spec.dest)


def test_adopt_creates_parent_directories(tmp_path):
    spec = _spec(tmp_path / "deep" / "nested")
    res = downloader.adopt(spec, _make(tmp_path, "m.bin"))
    assert res.ok
    assert os.path.exists(spec.dest)


def test_adopt_keeps_source_untouched(tmp_path):
    """收编不能删源文件——那可能是用户唯一的副本"""
    root = tmp_path / "data"
    spec = _spec(root)
    src = _make(tmp_path, "m.bin")
    before = open(src, "rb").read()

    downloader.adopt(spec, src)
    assert os.path.exists(src)
    assert open(src, "rb").read() == before


# --- 已就绪时不重复动作 -------------------------------------------------

def test_adopt_is_noop_when_already_ready(tmp_path):
    """已经在位且校验通过时，直接跳过（不该再链接一次）"""
    root = tmp_path / "data"
    spec = _spec(root)
    src = _make(tmp_path, "m.bin")

    assert downloader.adopt(spec, src).ok
    again = downloader.adopt(spec, src)
    assert again.ok
    assert again.skipped


def test_adopt_replaces_corrupt_dest(tmp_path):
    """目标位置是坏文件时要覆盖掉"""
    root = tmp_path / "data"
    spec = _spec(root)
    os.makedirs(os.path.dirname(spec.dest), exist_ok=True)
    with open(spec.dest, "wb") as f:
        f.write(b"corrupted-garbage-here")

    res = downloader.adopt(spec, _make(tmp_path, "m.bin"))
    assert res.ok
    assert downloader.sha256_file(spec.dest) == spec.sha256


# --- 目录扫描 -----------------------------------------------------------

def test_scan_finds_assets_by_hash_in_a_directory(tmp_path):
    """扫描一个目录，按**哈希**认出权重，不管文件名和层级怎么摆

    实测用户的文件散落在 `gguf/`、`gguf-mt2/`、`models/` 三个子目录里，
    靠文件名对是对不上的——只能靠哈希。
    """
    a1 = _fake_asset("alpha", b"AAAA-body-1")
    a2 = _fake_asset("beta", b"BBBB-body-22")
    scan = tmp_path / "library"
    p1 = _make(scan / "gguf", "Confucius4-R2T2-Q4_K_M.gguf", b"AAAA-body-1")
    p2 = _make(scan / "gguf-mt2", "Hy-MT2-1.8B-Q4_K_M.gguf", b"BBBB-body-22")
    # 文件名故意跟资产的 key 完全对不上 —— 就是为了证明认的是内容
    wanted = {a1.sha256: a1, a2.sha256: a2}

    found = downloader.scan_directory(str(scan), wanted)
    assert {f.key for f in found} == {"alpha", "beta"}
    assert {os.path.abspath(f.path) for f in found} == \
           {os.path.abspath(p1), os.path.abspath(p2)}


def test_scan_ignores_unrelated_files(tmp_path):
    a1 = _fake_asset("alpha", b"AAAA-body-1")
    scan = tmp_path / "mixed"
    _make(scan, "notes.txt", b"just some notes")
    _make(scan, "movie.mp4", b"fake video")
    assert downloader.scan_directory(str(scan), {a1.sha256: a1}) == []


def test_scan_reports_nothing_for_empty_dir(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    assert downloader.scan_directory(str(d)) == []


def test_scan_tolerates_missing_dir(tmp_path):
    """目录不存在时给空结果而不是抛异常（用户可能填错路径）"""
    assert downloader.scan_directory(str(tmp_path / "nope")) == []


def test_adopt_all_from_directory(tmp_path):
    """一步收编目录里所有能认出的权重"""
    asset = _fake_asset("alpha", b"AAAA-body-1")
    scan = tmp_path / "library"
    _make(scan / "gguf", "Confucius4-R2T2-Q4_K_M.gguf", b"AAAA-body-1")

    saved = registry.ASSETS
    try:
        registry.ASSETS = (asset,)
        spec = downloader.spec_for(asset, str(tmp_path / "data"))
        got = downloader.adopt_all_from(spec, str(scan))
    finally:
        registry.ASSETS = saved

    assert len(got) == 1
    assert got[0].ok, got[0].error
    assert os.path.exists(spec.dest)