"""模型资产接缝（`vidsub.models`）

这一层存在的理由是把散在四个模块里的协议收成一个接缝。所以测试也分两类：

1. **协议本身**：收编的顺序（先验源后动目标）、不删源、落协议全文、
   目录不存在要报错而"没找到"不算错
2. **成本控制**：这是本模块**唯一新增的判断**，也是最容易被改坏的地方 ——
   `inspect()` 默认不做全量哈希。实测全量校验 2.4 GB 要 2.17 秒，而首页
   分流、建作业闸口、下载页轮询都会调它。这里把"什么情况下信任磁盘"
   钉死，否则又会从别处长出一份重复的判定。

底层四模块的既有单测继续有效（它们现在是**内部接缝**）。
"""
import hashlib
import os

import pytest

from vidsub import discovery, downloader, models, registry


SIZE = 2048


def _body(key: str) -> bytes:
    """每个权重**内容必须不同**。

    四个权重内容一样的话哈希就一样，而 `scan_directory` 对同一份内容只认领
    一次 —— 症状是"只认出来一个，其余都说没找到"，且看起来像扫描坏了。
    体积也必须精确：认领先按体积粗筛。
    """
    seed = f"{key}-weight-body-".encode()
    out = (seed * (SIZE // len(seed) + 1))[:SIZE]
    assert len(out) == SIZE
    return out


BODY = _body("default")


def _asset(key: str) -> registry.Asset:
    body = _body(key)
    return registry.Asset(
        key=key, label=f"假权重 {key}", repo="o/r", path=f"{key}.bin",
        size_bytes=len(body), sha256=hashlib.sha256(body).hexdigest(),
        hf_repo="o/r", hf_path=f"{key}.bin")


@pytest.fixture
def fake_assets(monkeypatch):
    """把四个权重换成 2 KB 的假货，测试里不必造 2.6 GB。"""
    real = registry.ASSETS
    small = tuple(_asset(a.key) for a in real)
    monkeypatch.setattr(registry, "ASSETS", small)
    # discovery / downloader 各自 import 了 registry 模块本身，
    # 所以 patch registry.ASSETS 对它们同样生效（它们读的是属性不是副本）。
    return small


@pytest.fixture
def store(tmp_path, fake_assets):
    return models.ModelStore(str(tmp_path / "data"))


def _place(store, key, body=None):
    """把一个权重放到它的落盘位置"""
    asset = registry.get(key)
    spec = downloader.spec_for(asset, store.root)
    os.makedirs(os.path.dirname(spec.dest), exist_ok=True)
    with open(spec.dest, "wb") as f:
        f.write(_body(key) if body is None else body)
    return spec


def _count_hashes(monkeypatch):
    """记录 sha256 被调了几次 —— 成本控制只能这么验"""
    calls = []
    real = downloader.sha256_file

    def spy(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(downloader, "sha256_file", spy)
    return calls


# --- 构造：root 的约束 ---------------------------------------------------

def test_rejects_root_with_space(tmp_path):
    """含空格的模型目录会让 llama-server 拒绝启动，必须在构造时就挡住。

    与其等到起服务时报 invalid argument，不如早失败 —— 那时用户已经在
    界面上等了两分钟。
    """
    from vidsub import runtime
    with pytest.raises(runtime.SpaceInPathError):
        models.ModelStore(str(tmp_path / "has space" / "models"))


def test_rejects_empty_root():
    with pytest.raises(ValueError):
        models.ModelStore("")


def test_does_not_compute_root_itself(tmp_path):
    """root 由调用方传入，模块只校验。

    「用户名含空格要躲开」的逻辑全项目只能有一份（在 runtime 里）。
    历史上复制过一次，结果服务侧完全绕开了躲避逻辑。
    """
    s = models.ModelStore(str(tmp_path / "ok"))
    assert s.root == str(tmp_path / "ok")


# --- inspect：状态 -------------------------------------------------------

def test_inspect_all_missing(store, fake_assets):
    r = store.inspect()
    assert r.ready is False
    assert len(r.items) == len(fake_assets)
    assert set(i.state for i in r.items) == {models.STATE_MISSING}
    assert set(r.missing_keys) == {a.key for a in fake_assets}
    assert r.have_bytes == 0
    assert r.progress == 0.0


def test_inspect_ready_after_placing_all(store, fake_assets):
    for a in fake_assets:
        _place(store, a.key)
    r = store.inspect()
    assert r.ready is True
    assert r.missing_keys == ()
    assert r.have_bytes == r.total_bytes
    assert r.progress == 1.0


def test_inspect_reports_partial_by_size(store, fake_assets):
    """体积不足且没有边车 → corrupt（不是 partial）。

    partial 的定义很窄：只有"边车记录过、且长度对得上"才算真半成品。
    随手写个小文件进去不算。
    """
    spec = _place(store, fake_assets[0].key, body=BODY[:100])
    assert os.path.getsize(spec.dest) == 100
    r = store.inspect(verify=True)
    got = {i.key: i.state for i in r.items}
    assert got[fake_assets[0].key] == models.STATE_CORRUPT


def test_inspect_exposes_rel_path_for_llama_server(store, fake_assets):
    """路径解析是调用方的硬需求：喂 llama-server 的相对路径分隔符恒为 /"""
    r = store.inspect()
    for item in r.items:
        assert "/" in item.rel_path or item.rel_path
        assert "\\" not in item.rel_path, f"相对路径带了反斜杠：{item.rel_path}"
        assert item.abs_path.startswith(store.root)


# --- inspect：成本控制（本模块唯一新增的判断）-----------------------------

def test_default_inspect_does_not_hash(store, fake_assets, monkeypatch):
    """默认不重算哈希。这是整个模块最重要的性能约束。

    实测全量校验 2.4 GB 要 2.17 秒，而首页每次加载都会调 inspect()。
    """
    for a in fake_assets:
        _place(store, a.key)
    calls = _count_hashes(monkeypatch)

    r = store.inspect()

    assert r.ready is True
    assert calls == [], f"默认路径竟然算了 {len(calls)} 次哈希"


def test_verify_true_hashes(store, fake_assets, monkeypatch):
    """verify=True 是闸口语义：必须真校验"""
    for a in fake_assets:
        _place(store, a.key)
    calls = _count_hashes(monkeypatch)

    r = store.inspect(verify=True)

    assert r.ready is True
    assert len(calls) == len(fake_assets)


def test_repeat_inspect_does_not_hash_even_with_verify(
        store, fake_assets, monkeypatch):
    """校验过一次就记住 (体积, mtime)，之后连 verify=True 也不用重算。

    否则"校验一次"会退化成"每次校验"。
    """
    for a in fake_assets:
        _place(store, a.key)
    store.inspect(verify=True)
    calls = _count_hashes(monkeypatch)

    store.inspect(verify=True)

    assert calls == [], "第二次仍然重算了哈希"


def test_default_inspect_trusts_right_sized_file(store, fake_assets):
    """**刻意钉住这个取舍**：体积对得上但内容不对时，廉价路径认作 ready。

    代价说清楚：一份体积正好、内容已损坏的权重会被判成 ready。可接受 ——
    真损坏时 llama-server 加载会明确失败，错误里带日志尾巴，用户看到的是
    "服务起不来 + 原因"，而不是静默算错。

    这条测试的作用是：哪天有人想"顺手加个哈希"时，会先看到这里写了为什么
    不能加，以及加了要付什么代价。
    """
    a = fake_assets[0]
    _place(store, a.key, body=b"x" * SIZE)      # 体积对，内容错

    cheap = {i.key: i.state for i in store.inspect().items}
    assert cheap[a.key] == models.STATE_READY, "廉价路径应当信任磁盘"

    # 强制校验时才暴露真相
    store2 = models.ModelStore(store.root)
    strict = {i.key: i.state for i in store2.inspect(verify=True).items}
    assert strict[a.key] == models.STATE_CORRUPT


def test_verified_cache_invalidates_on_mtime_change(store, fake_assets):
    """文件被换掉后不能继续吃旧结论 —— 靠 mtime 失效，不需要手动清"""
    a = fake_assets[0]
    spec = _place(store, a.key)
    assert {i.key: i.state for i in store.inspect().items}[a.key] \
        == models.STATE_READY

    # 换成坏内容（体积故意不同，否则廉价路径仍会信任它）
    with open(spec.dest, "wb") as f:
        f.write(b"short")
    os.utime(spec.dest, (1, 1))         # 保证 mtime 变化，不依赖时钟精度

    got = {i.key: i.state for i in store.inspect(verify=True).items}
    assert got[a.key] == models.STATE_CORRUPT


# --- acquire：收编 -------------------------------------------------------

def test_acquire_from_directory(store, fake_assets, tmp_path):
    src = tmp_path / "src"
    for a in fake_assets:
        p = src / "nested" / f"{a.key}.bin"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_body(a.key))

    rep = store.acquire(str(src))

    assert rep.any_found is True
    assert all(o.found and o.ok for o in rep.outcomes)
    assert all(o.source_path for o in rep.outcomes)
    # 收编后的现状直接带回来，省掉紧跟着的一次 inspect
    assert rep.report.ready is True


def test_acquire_does_not_delete_source(store, fake_assets, tmp_path):
    """源文件永不删除：用户那份可能还有别的用途"""
    src = tmp_path / "src"
    p = src / f"{fake_assets[0].key}.bin"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(_body(fake_assets[0].key))

    store.acquire(str(src))

    assert p.exists(), "收编把源文件删了"


def test_acquire_missing_directory_raises(store, tmp_path):
    """目录不存在是**错误**（接口层转 400）"""
    with pytest.raises(models.SourceError):
        store.acquire(str(tmp_path / "nope"))


def test_acquire_empty_directory_is_result_not_error(store, fake_assets, tmp_path):
    """"目录在但一个都没认出来"是**结果**，不是异常。

    调用方据此转 404 并给出可读提示；抛异常会逼每个调用点写 try。
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    rep = store.acquire(str(empty))

    assert rep.any_found is False
    assert all(not o.found for o in rep.outcomes)
    assert all(o.error for o in rep.outcomes), "没找到时应当有可读原因"


def test_acquire_from_caches_reports_per_asset(store, fake_assets, monkeypatch):
    """标准缓存路径：**每个**权重都要有一条 outcome，包括没找到的。

    诊断脚本要靠这个逐条打印；少一条就等于报告里凭空少了东西。
    """
    found_key = fake_assets[0].key
    monkeypatch.setattr(discovery, "find_all",
                        lambda: {found_key: {"path": "/somewhere/x.bin",
                                             "source": "huggingface"}})
    adopted = []
    monkeypatch.setattr(downloader, "adopt",
                        lambda spec, src: adopted.append((spec.key, src))
                        or downloader.AdoptResult(ok=True, linked=True))

    rep = store.acquire()

    assert len(rep.outcomes) == len(fake_assets)
    assert [k for k, _ in adopted] == [found_key]
    ok = [o for o in rep.outcomes if o.ok]
    assert ok[0].linked is True
    assert ok[0].source == "huggingface"
    not_found = [o for o in rep.outcomes if not o.found]
    assert all("标准缓存" in o.error for o in not_found)


def test_acquire_writes_license_files(store, fake_assets, tmp_path, monkeypatch):
    """协议 3.4b：权重落盘时协议全文必须一起落盘。

    这条是收编契约的一部分 —— 调用方不需要、也不应该再单独调一次。
    """
    src = tmp_path / "src"
    p = src / f"{fake_assets[0].key}.bin"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(_body(fake_assets[0].key))

    called = []
    monkeypatch.setattr(downloader, "copy_license_files",
                        lambda root: called.append(root) or [])

    store.acquire(str(src))

    assert called == [store.root]


def test_acquire_does_not_write_license_when_nothing_adopted(
        store, fake_assets, tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    called = []
    monkeypatch.setattr(downloader, "copy_license_files",
                        lambda root: called.append(root) or [])

    store.acquire(str(empty))

    assert called == [], "一个都没收编却写了协议文件"


def test_verified_record_matches_disk_after_acquire(store, fake_assets, tmp_path):
    """收编后，校验记录里的 (体积, mtime) 必须对应**磁盘上现在这个文件**。

    否则就是拿"我验过旧文件"去担保新文件 —— 收编恰好换了内容时会给错 ready。
    （mtime 本身也是一道保险：文件被换掉后旧记录自然不匹配。这条测试把这个
    契约写下来，免得有人为了"省一次 stat"把它去掉。）
    """
    a = fake_assets[0]
    spec = _place(store, a.key)
    store.inspect()
    assert a.key in store._verified, "前置条件没建立"

    src = tmp_path / "src"
    p = src / f"{a.key}.bin"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(_body(a.key))
    os.utime(p, (1_000_000, 1_000_000))         # 明显不同的 mtime

    store.acquire(str(src))

    assert store._verified[a.key] == (os.path.getsize(spec.dest),
                                      os.path.getmtime(spec.dest)), \
        "校验记录与磁盘现状不符"


# --- Report 的对外形状（下载页契约）-------------------------------------

def test_report_as_dict_matches_download_page_contract(store, fake_assets):
    """下载页读的就是这个 dict。字段名改了页面会静默坏掉，所以钉死。

    这里断言的是**页面契约**（`items[].{have,total,note}`），不是内部
    AssetView 的字段名 —— 两者的映射由 as_dict() 负责，改内部字段不该
    影响页面。
    """
    d = store.inspect().as_dict()

    assert set(d) >= {"ready", "items", "missing", "caches", "hints",
                      "total_bytes", "have_bytes", "progress", "verified"}
    assert set(d["items"][0]) >= {"key", "label", "state", "have", "total",
                                  "note"}
    assert set(d["caches"]) == {"roots", "found"}
    assert set(d["caches"]["roots"][0]) == {"kind", "path", "exists", "env_var"}


def test_report_keeps_rich_asset_view_for_programmatic_callers(store, fake_assets):
    """`Report.items` 保留完整信息（路径/来源/用途），页面契约不替代它。

    下载页只要 have/total/note，但诊断脚本要 rel_path 喂 llama-server、
    要知道来源是哪个缓存 —— 这些不能因为迁就页面而丢掉。
    """
    r = store.inspect()
    item = r.items[0]
    assert item.rel_path and item.abs_path
    assert item.purpose is not None and item.license_note is not None
    assert r.missing_keys == tuple(a.key for a in fake_assets)


def test_report_serialises_without_surprises(store, fake_assets):
    """能直接进 JSONResponse —— 里面不能有 dataclass 或 set"""
    import json
    json.dumps(store.inspect().as_dict(), ensure_ascii=False)


def test_missing_assets_get_hints(store, fake_assets):
    """找不到时要给"去魔搭下载"的单文件可续传指引"""
    r = store.inspect()
    assert len(r.hints) == len(fake_assets)
    for h in r.hints:
        assert "-C -" in h["curl"], "指引丢了续传参数"
        assert h["size_text"]


# --- download：后台任务 --------------------------------------------------

def test_download_returns_task_handle(store, fake_assets):
    t = store.download()
    assert isinstance(t, models.Task)
    assert isinstance(t.id, str) and t.id
    assert set(t.progress()) >= {"state", "items"}


def test_idle_inspect_reports_idle_state_and_disk_items(store, fake_assets):
    """没有下载任务时，`state` 要是 idle、`items` 要回落到磁盘现状。

    页面无条件读 `state`/`error`/`items` 这三个字段；缺任何一个都会拿到
    undefined。契约稳定比少一个键重要 —— 这条钉住它。
    """
    r = store.inspect()
    assert r.download == {}, "没有任务时不该有任务快照"

    d = r.as_dict()
    assert d["state"] == "idle"
    assert d["error"] == ""
    assert len(d["items"]) == len(fake_assets), "没任务时要回落到磁盘现状"
