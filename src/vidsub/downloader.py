"""权重下载：断点续传 + sha256 校验 + 进度

为什么不用 `modelscope.snapshot_download`：
实测它**既不续传也不校验**。2.9GB 的权重下到一半断了就得从头再来，
而中途被截断的文件不会报错，只会在加载时莫名其妙失败。

这里的取舍：
- 续传靠 HTTP Range（已实测 ModelScope 返回 `Accept-Ranges: bytes`）
- 校验靠本项目自己锁定的 sha256（上游不发布校验值）
- 校验不过就**删掉**半成品：留着它，下次续传会拼出一个坏文件
- **下载之前先看本地有没有**（见 adopt/scan_directory）：用户不一定从零开始，
  已有权重应当收编而不是重下
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from . import registry

CHUNK = 1 << 20                      # 1 MiB

PROXY_ENV = "VIDSUB_DOWNLOAD_PROXY"   # 例如 http://127.0.0.1:7897
PROXY_HOSTS_ENV = "VIDSUB_PROXY_HOSTS"   # 追加需要走代理的域名，逗号分隔
FORCE_DIRECT_ENV = "VIDSUB_FORCE_DIRECT"  # 设 0 则全部走代理（墙内用）

# 直连就明确有问题的域名。实测 GitHub raw 直连会在 268KB 处截断
# （目标文件 2.3MB），静默拿到半个文件；走代理才完整。
# ModelScope **不在此列**：直连 35 MB/s，走代理只有 240 KB/s。
BUILTIN_PROXY_HOSTS = (
    "github.com",
    "githubusercontent.com",
    "githubassets.com",
)

_direct_openers: dict[bool, object] = {}
_direct_failed: set[str] = set()      # 记下"这些域名直连不行"


def _reset_route_cache() -> None:
    """测试用：环境变量变了要能重新决策"""
    _direct_openers.clear()
    _direct_failed.clear()


def _proxy_url() -> str:
    return (os.environ.get(PROXY_ENV) or "").strip()


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _proxy_hosts() -> tuple[str, ...]:
    extra = [h.strip().lower() for h in
             os.environ.get(PROXY_HOSTS_ENV, "").split(",") if h.strip()]
    return tuple(h.lower() for h in BUILTIN_PROXY_HOSTS) + tuple(extra)


def _use_proxy(url: str) -> bool:
    """这个 URL 要不要走代理。

    默认直连。三种情况走代理：
    1. `VIDSUB_FORCE_DIRECT=0`（用户明确要求，墙内场景）
    2. 域名在代理名单里（GitHub 实测直连会截断）
    3. 这个域名刚才直连失败过（自动回退，且记住以免每次重试都卡超时）
    """
    if not _proxy_url():
        return False
    if os.environ.get(FORCE_DIRECT_ENV, "1") == "0":
        return True
    host = _host_of(url).lower()
    if not host:
        return False
    if _direct_failed_hosts() and host in _direct_failed:
        return True
    return any(host == h or host.endswith("." + h) for h in _proxy_hosts())


def _direct_failed_hosts() -> bool:
    return bool(_direct_failed)


def _raw_open(url: str, start: int, use_proxy: bool):
    """真正发起一次请求。拆出来是为了测试能替换它。"""
    req = urllib.request.Request(url, headers={"User-Agent": "vidsub/0.1"})
    if start:
        req.add_header("Range", f"bytes={start}-")
    if use_proxy:
        proxy = _proxy_url()
        opener = _direct_openers.get(True)
        if opener is None:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            _direct_openers[True] = opener
    else:
        opener = _direct_openers.get(False)
        if opener is None:
            # 必须显式清空代理：否则会捡起 http_proxy 环境变量 / 系统设置，
            # 把请求绕到代理上去 —— 之前踩过，本地回环请求被劫持。
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            _direct_openers[False] = opener
    return opener.open(req, timeout=_timeout())


def _timeout() -> int:
    return int(os.environ.get("VIDSUB_DOWNLOAD_TIMEOUT", "60"))


def _open(url: str, start: int = 0):
    """按域名选路；直连失败（网络层）时自动回退代理。

    只对**网络层**错误回退：HTTP 4xx/5xx 说明请求本身有问题
    （404 是路径写错、416 是范围不合法），换个出口也是同样的结果，
    重试只会浪费时间并把真正的错误信息冲淡。
    """
    want_proxy = _use_proxy(url)
    try:
        return _raw_open(url, start, want_proxy)
    except urllib.error.HTTPError:
        raise                      # HTTP 错误不重试
    except (urllib.error.URLError, OSError, TimeoutError):
        if want_proxy or not _proxy_url():
            raise                  # 已经走代理了，或压根没代理可走
        host = _host_of(url).lower()
        if host:
            _direct_failed.add(host)      # 记住，别每次重试都卡满超时
        return _raw_open(url, start, True)


@dataclass(frozen=True)
class AssetSpec:
    key: str
    label: str
    url: str
    size_bytes: int
    sha256: str
    dest: str
    license_note: str = ""
    purpose: str = ""


@dataclass(frozen=True)
class Result:
    ok: bool
    skipped: bool = False
    resumed: bool = False
    error: str = ""
    bytes_written: int = 0


@dataclass(frozen=True)
class Status:
    state: str          # missing | partial | ready | corrupt
    have_bytes: int
    total_bytes: int

    @property
    def ratio(self) -> float:
        return 1.0 if self.state == "ready" else (
            self.have_bytes / self.total_bytes if self.total_bytes else 0.0)


def spec_for(asset: registry.Asset, root: str) -> AssetSpec:
    return AssetSpec(
        key=asset.key,
        label=asset.label,
        url=asset.url(),
        size_bytes=asset.size_bytes,
        sha256=asset.sha256,
        dest=os.path.join(root, registry.rel_path(asset).replace("/", os.sep)),
        license_note=asset.license_note,
        purpose=asset.purpose,
    )


def all_specs(root: str) -> list[AssetSpec]:
    return [spec_for(a, root) for a in registry.ASSETS]


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def status_of(spec: AssetSpec) -> Status:
    """看现状：缺 / 半成品 / 就绪 / 已损坏

    关键：**体积不足不等于半成品**。文件在但内容已损坏时（被别的程序改写、
    磁盘写入错乱等），若一律当 partial 续传，就会在坏文件后面不断追加，
    拼出更大的坏文件且永远校验不过。
    所以靠一份"已验证前缀"边车文件判断：只有记录过、且记录的长度与实际
    文件长度一致时，才算真半成品。
    """
    if not os.path.exists(spec.dest):
        return Status("missing", 0, spec.size_bytes)
    have = os.path.getsize(spec.dest)
    if have == 0:
        return Status("missing", 0, spec.size_bytes)
    if have > spec.size_bytes:
        return Status("corrupt", have, spec.size_bytes)
    if have == spec.size_bytes:
        if sha256_file(spec.dest) == spec.sha256.lower():
            return Status("ready", have, spec.size_bytes)
        return Status("corrupt", have, spec.size_bytes)
    if _verified_prefix(spec) == have:
        return Status("partial", have, spec.size_bytes)
    return Status("corrupt", have, spec.size_bytes)


def _sidecar(path: str) -> str:
    return path + ".partmeta"


def _verified_prefix(spec: AssetSpec) -> int:
    """读边车，返回"已验证可信的字节数"，读不到或对不上返回 -1。

    边车只在两种情况下写入：
      1. 每次续传追加之后，记录新的长度
      2. 下载完成校验通过、改名之后，删掉边车
    于是它记录的长度**就是**我们确信没被外部改写过的前缀长度。
    """
    try:
        with open(_sidecar(spec.dest), "r", encoding="utf-8") as f:
            meta = json.load(f)
        if meta.get("sha256") != spec.sha256.lower():
            return -1                     # 期望值变了，旧前缀不可信
        return int(meta["bytes"])
    except (OSError, ValueError, KeyError, TypeError):
        return -1


def _write_sidecar(spec: AssetSpec, nbytes: int) -> None:
    try:
        with open(_sidecar(spec.dest), "w", encoding="utf-8") as f:
            json.dump({"sha256": spec.sha256.lower(), "bytes": nbytes}, f)
    except OSError:
        pass          # 边车写不了只是失去续传能力，不该让下载失败


def _drop_sidecar(spec: AssetSpec) -> None:
    _silent_remove(_sidecar(spec.dest))


def _request(url: str, start: int = 0, timeout: int = 60):
    """兼容旧调用点。选路逻辑在 _open 里。"""
    return _open(url, start)


def download_one(spec: AssetSpec,
                 progress: Optional[Callable[[int, int], None]] = None,
                 retries: int = 3) -> Result:
    st = status_of(spec)
    if st.state == "ready":
        return Result(ok=True, skipped=True)

    os.makedirs(os.path.dirname(os.path.abspath(spec.dest)), exist_ok=True)

    # 只有"经边车确认过的真半成品"才续传；坏文件一律丢弃重来
    resuming = st.state == "partial"
    if st.state == "corrupt":
        _silent_remove(spec.dest)
        _drop_sidecar(spec)

    part = spec.dest + ".part"
    if resuming:
        try:
            os.replace(spec.dest, part)
        except OSError:
            resuming = False

    have = os.path.getsize(part) if os.path.exists(part) else 0
    last_err = ""

    for attempt in range(retries):
        try:
            # 体积已够但还没改名/校验时，别再发 Range 请求：
            # 直接续传会请求 bytes=<size>-，服务端回 416
            # （Requested Range Not Satisfiable），重试也全是 416，
            # 表现为"明明下满了却报错"。这种情况直接进校验环节。
            if have >= spec.size_bytes:
                return _finalize(spec, part, resuming)
            # 用落盘后的实际长度而不是 have + written：服务端若忽略 Range
            # 会整段重下，此时 have + written 是错的（会把旧半成品算进去）。
            have = _stream(url=spec.url, part=part, start=have,
                           append=bool(have), progress=progress,
                           total=spec.size_bytes)
            # 记下"这 have 字节是我们自己写下的"，供下次续传采信
            _write_sidecar(spec, have)
            if have != spec.size_bytes:
                last_err = (f"体积不符：得到 {have} 字节，"
                            f"应为 {spec.size_bytes} 字节（可能被截断）")
                continue
            return _finalize(spec, part, resuming)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_err = f"{type(e).__name__}: {e}"
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if attempt < retries - 1:
                continue

    return Result(ok=False, error=last_err or "下载失败", resumed=resuming)


def _finalize(spec: AssetSpec, part: str, resumed: bool) -> Result:
    """校验并就位。校验不过就删掉半成品 —— 留着会被下次拿来续传。"""
    have = os.path.getsize(part)
    if have != spec.size_bytes:
        return Result(ok=False, resumed=resumed, bytes_written=have,
                      error=(f"体积不符：得到 {have} 字节，"
                             f"应为 {spec.size_bytes} 字节（可能被截断）"))
    got = sha256_file(part)
    if got != spec.sha256.lower():
        _silent_remove(part)
        _drop_sidecar(spec)
        return Result(ok=False, resumed=resumed,
                      error=(f"sha256 校验失败：\n  期望 {spec.sha256}\n"
                             f"  实际 {got}"))
    os.replace(part, spec.dest)
    _drop_sidecar(spec)
    return Result(ok=True, resumed=resumed, bytes_written=have)


def _stream(url: str, part: str, start: int, append: bool,
            progress, total: int) -> int:
    """把响应体写进 part，返回**落盘后的总字节数**。

    返回总字节数而不是本次写入量：服务端若忽略 Range（返回 200 而非 206），
    本次写入的是整段，"总字节数"才不会把已丢弃的旧半成品算进来。
    """
    with _request(url, start=start) as resp:
        if start and resp.status != 206:
            # 服务端忽略了 Range：必须整段重下。若还按追加写，就会把整段
            # 数据接在旧半成品后面 —— 体积翻倍、内容错乱、永远校验不过。
            start, append = 0, False
        with open(part, "ab" if append else "wb") as f:
            if progress and start:
                progress(start, total)     # 先报已有量，进度条不闪回 0
            n = 0
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                n += len(chunk)
                if progress:
                    progress(start + n, total)
        return start + n


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# --- 收编本地已有的权重 --------------------------------------------------
#
# 用户不一定从零开始：手工下过、离线内网分发、多个项目共用一份，
# 都是常态。已经有就该直接用，不必再拉 2.6 GB。
#
# 认人**只认哈希**：实测用户的文件散落在 gguf/、gguf-mt2/、models/
# 三个子目录里，文件名与层级都对不齐清单，按路径匹配是对不上的。


@dataclass(frozen=True)
class Found:
    key: str
    label: str
    path: str            # 本地已有的那份
    sha256: str


@dataclass(frozen=True)
class AdoptResult:
    ok: bool
    skipped: bool = False
    linked: bool = False      # 是否走了硬链接（不占额外空间）
    error: str = ""


def adopt(spec: AssetSpec, source: str) -> AdoptResult:
    """把 `source` 收进 spec.dest。先校验，再放置。

    顺序很重要：**先验源文件的哈希，通过了才动目标位置**。
    反过来做的话，一次误操作就能把好文件覆盖成坏文件。

    放置优先硬链接：1.1 GB 的权重复制一份既慢又占空间，而同盘硬链接
    是零成本的。不支持时退回复制。
    """
    if not os.path.isfile(source):
        return AdoptResult(ok=False, error=f"文件不存在：{source}")

    try:
        if os.path.getsize(source) != spec.size_bytes:
            return AdoptResult(
                ok=False,
                error=(f"体积不符：{os.path.getsize(source)} 字节，"
                       f"应为 {spec.size_bytes} 字节"))
        got = sha256_file(source)
        if got != spec.sha256.lower():
            return AdoptResult(ok=False, error=f"sha256 校验失败：\n  期望 "
                                                 f"{spec.sha256}\n  实际 {got}")
    except OSError as e:
        return AdoptResult(ok=False, error=f"读取失败：{e}")

    # 已经在位且就是这份源文件（或同样内容），不必再动
    if os.path.exists(spec.dest):
        try:
            if os.path.samefile(spec.dest, source):
                return AdoptResult(ok=True, skipped=True, linked=True)
        except OSError:
            pass
        if os.path.getsize(spec.dest) == spec.size_bytes \
                and sha256_file(spec.dest) == spec.sha256.lower():
            return AdoptResult(ok=True, skipped=True)

    os.makedirs(os.path.dirname(os.path.abspath(spec.dest)), exist_ok=True)
    tmp = spec.dest + ".adopting"      # 先落临时名，避免半途失败留下半个文件

    linked = True
    try:
        os.link(source, tmp)           # 零拷贝
    except OSError:
        linked = False                  # 跨盘 / Windows 无权限 / 文件系统不支持
        try:
            shutil.copy2(source, tmp)
        except OSError as e:
            _silent_remove(tmp)
            return AdoptResult(ok=False, error=f"复制失败：{e}")

    _silent_remove(spec.dest)           # 旧的坏文件
    try:
        os.replace(tmp, spec.dest)
    except OSError as e:
        _silent_remove(tmp)
        return AdoptResult(ok=False, error=f"放置失败：{e}")

    return AdoptResult(ok=True, linked=linked)


def _walk_files(root: str, min_bytes: int, max_bytes: int):
    """递归列出候选文件，按体积粗筛。

    粗筛是**性能**考虑，不是正确性考虑：扫描一个大目录时，先用体积
    挡掉绝大多数文件（笔记、截图、视频），只对体积对得上的算哈希。

    体积区间取自调用方的资产列表，而不是写死下限 —— 写死会在遇到
    小权重时静默漏掉它，且症状极隐蔽：明明文件就在那儿，却报"没找到"。
    """
    if not os.path.isdir(root):
        return
    for base, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(base, name)
            try:
                n = os.path.getsize(p)
            except OSError:
                continue
            if min_bytes <= n <= max_bytes:
                yield p


def scan_directory(root: str, wanted: Optional[dict] = None) -> list[Found]:
    """扫一个**用户指定**的目录，按哈希认出里面有哪些权重。

    只在用户明确指了目录时用（如"我下好了，在这"）。自动探测一律走
    discovery 模块的标准缓存目录，不来这里瞎扫 —— 整个 home 扫一遍
    既慢又吵。

    按内容认人而不是按文件名：权重可能被改过名、放在任意层级。
    先按体积筛候选，再只对体积对得上的算哈希，避免对整个目录做哈希。
    """
    targets = wanted or {a.sha256: a for a in registry.ASSETS}
    by_size: dict[int, list] = {}
    for sha, a in targets.items():
        by_size.setdefault(a.size_bytes, []).append((sha, a))
    if not by_size:
        return []

    out: list[Found] = []
    claimed: set[str] = set()      # 已认领掉的文件
    claimed_sha: set[str] = set()  # 已认领掉的哈希
    for path in _walk_files(root, min(by_size), max(by_size)):
        try:
            size = os.path.getsize(path)
            cands = by_size.get(size)
            if not cands:
                continue
            got = sha256_file(path)
        except OSError:
            continue
        if got in claimed_sha:
            continue          # 同一份内容只认领一次
        for sha, a in cands:
            if sha == got:
                out.append(Found(key=a.key, label=a.label, path=path,
                                 sha256=got))
                claimed.add(os.path.abspath(path))
                claimed_sha.add(got)
                break
    return out


def adopt_all_from(spec: AssetSpec, scan_root: str) -> list[AdoptResult]:
    """把扫描到的、与 spec 对得上的那个收编进来"""
    hits = [f for f in scan_directory(scan_root) if f.key == spec.key]
    if not hits:
        return [AdoptResult(ok=False, error="目录里没找到可用的文件")]
    return [adopt(spec, hits[0].path)]


def copy_license_files(root: str) -> list[str]:
    """把第三方协议全文复制到数据目录。

    网易协议 3.4b 条要求「在使用的每一份模型或衍生作品副本中，保留所有
    原始版权声明及本协议副本」——所以下载完权重要把协议一起放好。
    """
    out = []
    here = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    for name, _url in registry.LICENSE_FILES.values():
        src = os.path.join(here, name)
        if not os.path.exists(src):
            continue
        dst = os.path.join(root, "licenses", name)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        out.append(dst)
    return out
