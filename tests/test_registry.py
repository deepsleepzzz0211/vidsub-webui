"""模型清单的自洽性

这一层是整个下载系统的基础：清单错了，后面所有校验都是假的。
所以对清单本身的检查要严格 —— 它是唯一"信任根"。
"""
import pytest

from vidsub import registry


def test_all_four_assets_declared():
    """识别主模型、音频侧投影、翻译模型、VAD 四件套齐全"""
    names = [a.key for a in registry.ASSETS]
    assert len(names) == 4, f"应有 4 个权重，实际 {names}"
    for required in ("asr_model", "asr_mmproj", "mt_model", "vad"):
        assert required in names


def test_assets_have_unique_keys():
    keys = [a.key for a in registry.ASSETS]
    assert len(keys) == len(set(keys)), "key 重复"


def test_org_names_are_case_sensitive_and_correct():
    """组织名大小写敏感：Tencent-Hunyuan 存在，tencent 会 404。

    这是实测踩过的坑 —— 清单里大小写写错时下载直接失败，且报错信息
    是 404，看不出是大小写问题。
    """
    by_key = {a.key: a for a in registry.ASSETS}
    assert by_key["asr_model"].repo == "netease-youdao/Confucius4-R2T2-GGUF"
    # 关键：Hunyuan 的 H 与 y 必须大写
    assert by_key["mt_model"].repo.startswith("Tencent-Hunyuan/")
    assert by_key["mt_model"].repo != by_key["mt_model"].repo.lower()


def test_filenames_match_their_repo():
    """文件名与所属仓库要对应，避免把 A 仓库的文件名配到 B 仓库"""
    for a in registry.ASSETS:
        assert a.repo.count("/") == 1, f"{a.key} 的 repo 格式不对：{a.repo}"
        assert a.path, f"{a.key} 缺少文件路径"
        if a.source == registry.Source.DIRECT:
            assert a.direct_url.startswith("https://"), \
                f"{a.key} 是直链来源，必须提供 direct_url"
        else:
            assert a.repo.split("/")[0] in a.modelscope_url()


def test_vad_uses_pinned_tag_not_master():
    """VAD 必须锁 v5.1.2 标签 —— master 是 v6.x，接口变了会静默失效"""
    vad = registry.get("vad")
    assert "v5.1.2" in vad.direct_url
    assert "master" not in vad.direct_url
    assert "main" not in vad.direct_url


def test_every_asset_has_a_usable_url():
    for a in registry.ASSETS:
        assert a.url().startswith("https://"), f"{a.key} 默认源 URL 不合法"
        # 备用源也要能用
        assert a.url(registry.Source.HUGGINGFACE).startswith("https://")


def test_default_source_is_modelscope_where_available():
    """默认走 ModelScope（实测 HF 直连不通、ModelScope 35MB/s）"""
    for a in registry.ASSETS:
        if a.key == "vad":
            assert a.source == registry.Source.DIRECT
        else:
            assert a.source == registry.Source.MODELSCOPE
            assert "modelscope.cn" in a.url()


def test_every_asset_has_sha256():
    """每个权重都要有锁定哈希 —— 上游不发布校验值，只能我们自己记"""
    for a in registry.ASSETS:
        assert len(a.sha256) == 64, f"{a.key} 的 sha256 长度不对"
        assert all(c in "0123456789abcdefABCDEF" for c in a.sha256)


def test_sizes_are_plausible():
    """体积必须是正数，且 VAD 那种小模型不能标成几百 MB"""
    for a in registry.ASSETS:
        assert a.size_bytes > 0, f"{a.key} 体积未填"
    vad = next(a for a in registry.ASSETS if a.key == "vad")
    assert vad.size_bytes < 50 * 1024 * 1024, "VAD 模型不该超过 50MB"


def test_asr_model_and_mmproj_come_from_same_repo():
    """识别主模型与音频侧投影必须同源，否则版本可能不匹配"""
    by_key = {a.key: a for a in registry.ASSETS}
    assert by_key["asr_model"].repo == by_key["asr_mmproj"].repo


def test_hf_mirror_is_available_for_every_asset():
    """每个权重都要有 HuggingFace 备用源（默认 ModelScope，但留逃生口）"""
    for a in registry.ASSETS:
        assert a.hf_repo and a.hf_path, f"{a.key} 缺 HuggingFace 备用源"


def test_rel_paths_have_no_spaces():
    """落盘相对路径不能含空格 —— 含空格的绝对路径会让 llama-server 拒绝启动"""
    for a in registry.ASSETS:
        p = registry.rel_path(a)
        assert " " not in p, f"{a.key} 的落盘路径含空格：{p}"


def test_asr_model_and_mmproj_kept_together():
    """两个识别权重落在同一目录，便于以相对路径传给 llama-server"""
    by = {a.key: registry.rel_path(a) for a in registry.ASSETS}
    assert by["asr_model"].startswith("r2t2/")
    assert by["asr_mmproj"].startswith("r2t2/")


def test_r2t2_asset_carries_license_notice():
    """R2T2 是自定义协议，清单里必须挂上协议提醒"""
    r2t2 = [a for a in registry.ASSETS if "R2T2" in a.repo or "R2T2" in a.path]
    assert r2t2, "应至少有一个 R2T2 资产"
    for a in r2t2:
        assert a.license_note, f"{a.key} 缺许可说明"
        assert "NetEase" in a.license_note or "网易" in a.license_note


def test_get_by_key():
    a = registry.get("mt_model")
    assert a.key == "mt_model"
    with pytest.raises(KeyError):
        registry.get("no-such-asset")


def test_total_size_reported():
    total = registry.total_size_bytes()
    # 实测：1107404736 + 348336544 + 1133080448 + 2327524 = 2,591,149,252 字节
    # （2.41 GiB / 2.59 GB）。别用"2.9GB"这种约数——那是按 GiB 猜的，会算错。
    assert total == 2591149252, f"合计 {total} 与锁定值不符"
    assert 2 * 1024 ** 3 < total < 3 * 1024 ** 3
