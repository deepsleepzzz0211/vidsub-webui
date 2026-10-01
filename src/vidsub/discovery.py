"""在官方缓存目录里找已有的权重

**不去自造目录里瞎搜。** 要找的是"用户早就下过的模型会躺在哪儿"，
答案就是 HuggingFace 和 ModelScope 自己的缓存位置：

HuggingFace（`~/.cache/huggingface/hub`，可用 `HF_HOME` / `HF_HUB_CACHE` 改）::

    models--<org>--<repo>/snapshots/<版本>/<文件>   # 文件是符号链接指回 blobs/
    models--<org>--<repo>/<文件>                    # 早期版本直接在根下

ModelScope 1.37（`~/.cache/modelscope/hub`，可用 `MODELSCOPE_CACHE` 改）::

    <org>/<repo>/<文件>                             # 本机实测就是这个形态
    models--<org>--<repo>/snapshots/<版本>/<文件>     # 旧版形态，一并认

两种 ModelScope 形态都要认：本机装的是 1.37（`hub/<org>/<repo>`），
但它自己的 `cache_manager` docstring 里还写着旧的 `models--org--name`，
与代码矛盾 —— 说明版本差异真实存在，只认一种会在别的机器上漏掉。

**定点找，不遍历。** 缓存目录动辄几十 GB、上万个无关文件，遍历一遍要几十秒。
而每个权重的落点是确定的，直接按规则算出来比对，毫秒级完成。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from glob import glob
from typing import Optional

from . import registry

HF = "huggingface"
MS = "modelscope"


@dataclass(frozen=True)
class CacheRoot:
    kind: str
    path: str
    exists: bool
    env_var: str = ""      # 实际生效的环境变量；空表示走的是默认位置


def _home() -> str:
    return os.path.expanduser("~")


def cache_roots() -> list[CacheRoot]:
    """官方缓存根目录。顺序即优先级：显式环境变量 > 默认位置。"""
    out: list[CacheRoot] = []

    hf_hub = os.environ.get("HF_HUB_CACHE")
    env_var = "HF_HUB_CACHE"
    if not hf_hub:
        hf_home = os.environ.get("HF_HOME")
        env_var = "HF_HOME" if hf_home else ""     # 没设就别声称是它决定的
        hf_hub = os.path.join(hf_home, "hub") if hf_home else \
            os.path.join(_home(), ".cache", "huggingface", "hub")
    out.append(CacheRoot(HF, os.path.normpath(hf_hub),
                         os.path.isdir(hf_hub), env_var))

    ms_env = os.environ.get("MODELSCOPE_CACHE")
    ms = ms_env or os.path.join(_home(), ".cache", "modelscope", "hub")
    out.append(CacheRoot(MS, os.path.normpath(ms), os.path.isdir(ms),
                         "MODELSCOPE_CACHE" if ms_env else ""))
    return out


def _roots_of(kind: str) -> list[CacheRoot]:
    return [r for r in cache_roots() if r.kind == kind]


def _sized_ok(path: str, expect: int) -> bool:
    """体积必须精确对上。

    这是防"认错量化版本"的关键：同一仓库里常有 Q4_K_M 与 Q8_0 等多个
    量化，文件名不同但都可能以 `.gguf` 结尾；只按名字认会拿到跑不起来的权重。
    符号链接要用 `getsize`（会穿透），实测 HF 的快照文件就是链接。
    """
    try:
        return os.path.isfile(path) and os.path.getsize(path) == expect
    except OSError:
        return False


def _flat_dir(org: str, repo: str) -> str:
    """HF / 旧版 ModelScope 的仓库目录名：把 `org/name` 变成 `models--org--name`

    必须用 `/` 硬编码，不能拿 `os.sep` 拼再 replace：Windows 上
    `os.sep` 是 `\\`，`replace("/","--")` 根本不命中，拼出来还是
    `org\\name`，去找 `models--org\\name` 自然什么都找不到。
    """
    return f"models--{org}/{repo}".replace("/", "--")


def _candidates(root: CacheRoot, org: str, repo: str,
                fname: str, size: int) -> list[str]:
    """按已知的几种布局，算出这个权重的预期落点（只算，不遍历）"""
    out: list[str] = []
    parts = fname.split("/")

    def add(p: str) -> None:
        if _sized_ok(p, size) and p not in out:
            out.append(p)

    flat = _flat_dir(org, repo)

    if root.kind == HF:
        base = os.path.join(root.path, flat)
        add(os.path.join(base, *parts))                          # 早期版本直接放根下
        for snap in sorted(glob(os.path.join(base, "snapshots", "*"))):
            add(os.path.join(snap, *parts))                      # 常规：按版本快照
    else:
        add(os.path.join(root.path, org, repo, *parts))          # 1.37
        base = os.path.join(root.path, flat)                     # 旧版布局
        add(os.path.join(base, *parts))
        for snap in sorted(glob(os.path.join(base, "snapshots", "*"))):
            add(os.path.join(snap, *parts))

    return out


def _search(asset: registry.Asset, kinds: Optional[list[str]] = None) -> list[str]:
    """在指定来源的缓存目录里找。默认两处都找。"""
    kinds = kinds or [HF, MS]
    out: list[str] = []
    for root in cache_roots():
        if root.kind not in kinds or not root.exists:
            continue
        # HF 用 hf_repo，ModelScope 用 repo —— 仓库 ID 不一定相同
        org, _, repo = asset.hf_repo.rpartition("/") \
            if root.kind == HF else asset.repo.rpartition("/")
        if not org:
            continue
        fname = asset.hf_path if root.kind == HF else asset.path
        for p in _candidates(root, org, repo, fname, asset.size_bytes):
            if p not in out:
                out.append(p)
    return out


def find_asset(asset: registry.Asset) -> Optional[str]:
    hits = _search(asset)
    return hits[0] if hits else None


def find_all() -> dict:
    """清单里每个权重在标准缓存里的下落。键是 weight key。"""
    out: dict = {}
    for a in registry.ASSETS:
        hits = _search(a)
        if hits:
            out[a.key] = {"path": hits[0], "source": _origin(hits[0])}
    return out


def _origin(path: str) -> str:
    """报告这份是从哪个缓存来的，页面上要能说明白"""
    norm = os.path.normpath(path).lower()
    for root in cache_roots():
        if norm.startswith(os.path.normpath(root.path).lower() + os.sep.lower()):
            return root.kind
    return "unknown"


def human_size(n: int) -> str:
    """给用户看的体积。

    与下载页上的 `fmt()` 保持同一套规则，且**2 GB 以内一律用 MB**：
    1.1 GB 的权重按 1024 进位会写成 "1.0 GB"，看不出是 1056 还是 1024，
    用户对不上自己那份文件的体积。总量超过 2 GB 才切 GB。
    """
    mb = n / 1024 ** 2
    if mb < 2048:
        return f"{n / 1024:.0f} KB" if mb < 0.1 else f"{mb:.1f} MB"
    return f"{mb / 1024:.2f} GB"


def download_hint(asset: registry.Asset, dest_root: str = "") -> dict:
    """给"去魔搭下载"的指引。

    只给**单文件**的可续传下载命令，不让用户 `modelscope download --model`
    整个仓库：GGUF 仓库里同时躺着 Q4_K_M / Q8_0 / f16，整个下要多花
    好几倍磁盘和时间，而我们只要其中一个文件。
    """
    name = os.path.basename(asset.path)
    url = asset.modelscope_url() if asset.source != registry.Source.DIRECT \
        else asset.direct_url
    target_dir = dest_root or "<模型目录>"
    target = os.path.join(target_dir, name)

    return {
        "key": asset.key,
        "label": asset.label,
        "repo": asset.repo,
        "path": asset.path,
        "size_bytes": asset.size_bytes,
        "size_text": human_size(asset.size_bytes),
        "url": url,
        "target": target,
        # -C - 让中断能续传，和内置下载器行为一致
        "curl": f'curl -L -C - -o "{target}" "{url}"',
        "note": "放到 vidsub 的模型目录后刷新本页即可识别；"
                "文件名不必一致，会按 sha256 校验后认领。",
    }


def hints_for_missing(keys) -> list[dict]:
    """给缺失的权重批量生成指引"""
    out = []
    for k in keys:
        try:
            out.append(download_hint(registry.get(k)))
        except KeyError:
            continue
    return out