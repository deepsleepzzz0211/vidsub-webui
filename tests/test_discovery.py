"""在标准缓存目录里找已有的权重

用户说得很对：**不该去 `gguf/`、`models/` 这类自造目录里瞎搜**。
真正值得找的是"官方会自己下载到哪儿"的那几个地方：

- HuggingFace：`~/.cache/huggingface/hub/models--<org>--<repo>/snapshots/<版本>/<文件>`
  （可用 `HF_HOME` / `HF_HUB_CACHE` 改）
- ModelScope 1.37：`~/.cache/modelscope/hub/<org>/<repo>/<文件>`（可用 `MODELSCOPE_CACHE` 改）
- ModelScope 旧版：`~/.cache/modelscope/hub/models--<org>--<repo>/...`

实测确认：
- 本机 HF 缓存确实在 `C:/Users/<用户>/.cache/huggingface/hub`，里面
  `model.bin` 等是**符号链接**指回 `blobs/`，`getsize` 能穿透读到真实大小
- 本机 ModelScope 1.37 用的是 `hub/<org>/<repo>` 形式（它自己
  cache_manager 的 docstring 里还写着旧的 `models--org--name`，与代码矛盾，
  所以两种都得认）

找法是**按预期路径定点找**，不是遍历整个缓存目录：
缓存目录动辄几十 GB、无关文件上万个，遍历会慢到没法用；而每个权重的
落点是确定的，直接算出来比对即可。
"""
import os

import pytest

from vidsub import discovery, registry


def _touch(path, size=1024, body=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(body * (size // len(body) + 1))
    with open(path, "r+b") as f:      # 截到精确体积
        f.truncate(size)
    return path


@pytest.fixture
def hf_home(tmp_path, monkeypatch):
    d = tmp_path / "hf"
    monkeypatch.setenv("HF_HOME", str(d))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    return d


@pytest.fixture
def ms_home(tmp_path, monkeypatch):
    d = tmp_path / "ms"
    monkeypatch.setenv("MODELSCOPE_CACHE", str(d))
    return d


# --- 缓存根目录 ---------------------------------------------------------

def test_hf_root_follows_hf_home(hf_home):
    roots = [r for r in discovery.cache_roots() if r.kind == "huggingface"]
    assert roots, "应认出 HF 缓存根"
    assert str(hf_home) in roots[0].path


def test_hf_hub_cache_overrides_hf_home(hf_home, tmp_path, monkeypatch):
    """"HF_HUB_CACHE" 直接指 hub 目录，优先于 HF_HOME"""
    hub = tmp_path / "custom-hub"
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    roots = [r for r in discovery.cache_roots() if r.kind == "huggingface"]
    assert roots[0].path == str(hub)


def test_modelscope_root_follows_env(ms_home):
    roots = [r for r in discovery.cache_roots() if r.kind == "modelscope"]
    assert roots and str(ms_home) in [r.path for r in roots]


def test_cache_roots_mark_existence(hf_home, ms_home):
    roots = discovery.cache_roots()
    hf = next(r for r in roots if r.kind == "huggingface")
    assert hf.exists is False          # 目录还没建
    os.makedirs(hf.path, exist_ok=True)
    assert next(r for r in discovery.cache_roots()
                if r.kind == "huggingface").exists is True


def test_real_user_paths_are_included_without_env(tmp_path, monkeypatch):
    """不给任何环境变量时，也要认官方默认位置"""
    for v in ("HF_HOME", "HF_HUB_CACHE", "MODELSCOPE_CACHE"):
        monkeypatch.delenv(v, raising=False)
    paths = [r.path for r in discovery.cache_roots()]
    joined = " ".join(paths).replace("\\", "/")
    assert ".cache/huggingface/hub" in joined
    assert ".cache/modelscope/hub" in joined


# --- 定点查找 -----------------------------------------------------------

def test_finds_in_hf_snapshot_layout(hf_home, monkeypatch):
    """HF 的 snapshots/<版本>/ 下按文件名找"""
    a = _small_asset("alpha", hf_path="alpha.gguf")
    _touch(str(hf_home / "hub" / "models--Org--Alpha" / "snapshots" /
               "abc123def" / "alpha.gguf"), a.size_bytes)
    monkeypatch.setattr(registry, "ASSETS", (a,))
    monkeypatch.setattr(discovery.registry, "ASSETS", (a,))

    found = discovery.find_asset(a)
    assert found is not None
    assert os.path.basename(found) == "alpha.gguf"
    assert os.path.getsize(found) == a.size_bytes


def test_finds_in_hf_blobs_style_direct_layout(hf_home, monkeypatch):
    """HF 有些版本直接把文件放在仓库目录根下（没有 snapshots）"""
    a = _small_asset("alpha", hf_path="alpha.gguf")
    _touch(str(hf_home / "hub" / "models--Org--Alpha" / "alpha.gguf"),
           a.size_bytes)
    monkeypatch.setattr(discovery.registry, "ASSETS", (a,))

    assert discovery.find_asset(a) is not None


def test_finds_in_modelscope_current_layout(ms_home, monkeypatch):
    """ModelScope 1.37：hub/<org>/<repo>/<文件>"""
    a = _small_asset("alpha")
    _touch(str(ms_home / "org" / "alpha" / "alpha.gguf"), a.size_bytes)
    monkeypatch.setattr(discovery.registry, "ASSETS", (a,))

    found = discovery.find_asset(a)
    assert found and os.path.basename(found) == "alpha.gguf"


def test_finds_in_modelscope_legacy_snapshot_layout(ms_home, monkeypatch):
    """ModelScope 旧版：hub/models--<org>--<repo>/snapshots/<版本>/<文件>"""
    a = _small_asset("alpha")
    _touch(str(ms_home / "models--org--alpha" / "snapshots" / "rev1" /
               "alpha.gguf"), a.size_bytes)
    monkeypatch.setattr(discovery.registry, "ASSETS", (a,))

    assert discovery.find_asset(a) is not None


def test_picks_newest_snapshot_when_several(hf_home, monkeypatch):
    """同一仓库有多个版本快照时，取文件真实存在的那个即可（内容一致）"""
    a = _small_asset("alpha", hf_path="alpha.gguf")
    repo = hf_home / "hub" / "models--Org--Alpha" / "snapshots"
    os.makedirs(repo / "old", exist_ok=True)      # 旧版本缺这个文件
    _touch(str(repo / "new" / "alpha.gguf"), a.size_bytes)
    monkeypatch.setattr(discovery.registry, "ASSETS", (a,))

    assert discovery.find_asset(a) is not None


def test_returns_none_when_absent(hf_home, ms_home, monkeypatch):
    a = _small_asset("alpha")
    monkeypatch.setattr(discovery.registry, "ASSETS", (a,))
    assert discovery.find_asset(a) is None


def test_ignores_wrong_size_lookalike(hf_home, monkeypatch):
    """同名但体积不对的文件不算数 —— 免得把别的量化版本认成我们要的

    真实存在这种坑：仓库里同时有 Q4_K_M 与 Q8_0，文件名不同但都叫
    `<name>.gguf`；认错体积会拿到跑不起来的权重。
    """
    a = _small_asset("alpha")
    _touch(str(hf_home / "hub" / "models--Org--Alpha" / "alpha.gguf"),
           a.size_bytes + 4096)
    monkeypatch.setattr(discovery.registry, "ASSETS", (a,))
    assert discovery.find_asset(a) is None


def test_does_not_walk_unrelated_cache_content(hf_home, monkeypatch):
    """缓存里大量无关文件也不该被误认

    关键：不遍历整个缓存目录，只认预期路径。
    """
    a = _small_asset("alpha")
    noise = hf_home / "hub" / "models--Other--Thing" / "snapshots" / "r"
    _touch(str(noise / "alpha.gguf"), a.size_bytes)     # 同名同体积，但在别的仓库
    monkeypatch.setattr(discovery.registry, "ASSETS", (a,))
    assert discovery.find_asset(a) is None


# --- 汇总 ---------------------------------------------------------------

def test_find_all_reports_each_asset(hf_home, ms_home, monkeypatch):
    a1 = _small_asset("alpha")
    a2 = _small_asset("beta")
    _touch(str(hf_home / "hub" / "models--Org--Alpha" / "snapshots" / "r" /
               "alpha.gguf"), a1.size_bytes)
    _touch(str(ms_home / "org" / "beta" / "beta.gguf"), a2.size_bytes)
    monkeypatch.setattr(discovery.registry, "ASSETS", (a1, a2))

    got = discovery.find_all()
    assert set(got) == {"alpha", "beta"}
    assert got["alpha"]["path"].endswith("alpha.gguf")


def test_find_all_is_empty_on_clean_machine(monkeypatch, tmp_path):
    for v in ("HF_HOME", "HF_HUB_CACHE", "MODELSCOPE_CACHE"):
        monkeypatch.setenv(v, str(tmp_path / v))
    assert discovery.find_all() == {}


# --- 找不到时给出"去魔搭下载"的指引 ------------------------------------

def test_hint_names_the_modelscope_repo(hf_home, ms_home, monkeypatch):
    a = _small_asset("alpha")
    monkeypatch.setattr(discovery.registry, "ASSETS", (a,))
    hint = discovery.download_hint(a)
    assert hint["repo"] == a.repo
    assert hint["size_bytes"] == a.size_bytes
    assert "modelscope" in hint["url"].lower()


def test_hint_gives_a_resumable_curl_command(monkeypatch, tmp_path):
    """指引要给**能续传的单文件下载**，不要让用户下整个仓库

    真实坑：GGUF 仓库里同时有 Q4_K_M / Q8_0 / f16，整个仓库下下来
    多花好几倍磁盘和时间，而我们只要其中一个文件。
    """
    a = _small_asset("alpha")
    hint = discovery.download_hint(a)
    assert "-C -" in hint["curl"], "curl 要带续传标志（-C -）"
    assert a.path in hint["curl"], "curl 要指名具体文件，不是整个仓库"
    assert "modelscope download --model" not in hint["curl"]


def test_hint_includes_a_target_dir_free_of_spaces(ms_home, monkeypatch):
    """落盘目录不能含空格 —— 含空格的绝对路径会让 llama-server 拒绝启动"""
    a = _small_asset("alpha")
    hint = discovery.download_hint(a)
    target = hint["target"]
    assert " " not in target


def test_every_real_asset_has_a_usable_hint():
    """清单里每个权重都要能生成指引（这是兜底路径，不能缺）"""
    for a in registry.ASSETS:
        h = discovery.download_hint(a)
        assert h["url"].startswith("https://")
        assert h["repo"] and h["size_bytes"] > 0
        assert h["curl"] and "-C -" in h["curl"]


def _sha(body: bytes) -> str:
    import hashlib
    return hashlib.sha256(body).hexdigest()


def _small_asset(key="alpha", body=b"tiny-model-bytes", hf_path=None,
                 repo=None):
    """体积很小的假资产：真实权重 1.1GB，测试里没法造

    仓库名默认跟着 key 走 —— 否则多个资产的 repo 会撞成同一个，
    找的时候互相干扰，症状是"只找到一个"。
    """
    repo = repo or f"org/{key}"
    return registry.Asset(
        key=key, label=f"假权重 {key}", repo=repo,
        path=hf_path or f"{key}.gguf", size_bytes=len(body),
        sha256=_sha(body),
        hf_repo=repo, hf_path=hf_path or f"{key}.gguf")