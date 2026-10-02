"""模型清单：整个下载系统的信任根

上游**不发布校验值**（ModelScope 与 HuggingFace 都没有 sha256），所以
每个权重的哈希只能由我们自己锁定并维护。这是唯一的信任根 —— 它错了，
下面所有校验都是假的。

⚠️ 换权重时必须重算哈希：先删掉旧的、再按本文件的 URL 重新下载，
然后用 scripts/refresh_hashes.py 更新这里。不要凭记忆改哈希。

**组织名大小写敏感**：`Tencent-Hunyuan/...` 存在，`tencent/...` 会 404。
这是实测踩过的坑，且 404 报错完全看不出是大小写问题。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Source(str, Enum):
    MODELSCOPE = "modelscope"
    HUGGINGFACE = "huggingface"
    DIRECT = "direct"          # 权重不在任何仓库托管，只能按 URL 取


@dataclass(frozen=True)
class Asset:
    key: str
    label: str
    repo: str                      # ModelScope: 组织/仓库
    path: str                      # 仓库内文件路径
    size_bytes: int
    sha256: str
    hf_repo: str                   # 备用源（HuggingFace）
    hf_path: str
    source: Source = Source.MODELSCOPE
    license_note: str = ""
    purpose: str = ""
    # 少数权重不在 ModelScope 上（例如 Silero VAD 实测三个候选仓库均无此文件），
    # 这类直接给完整 URL。
    direct_url: str = ""

    def modelscope_url(self, revision: str = "master") -> str:
        return ("https://www.modelscope.cn/api/v1/models/"
                f"{self.repo}/repo?Revision={revision}&FilePath={self.path}")

    def hf_url(self, revision: str = "main") -> str:
        return (f"https://huggingface.co/{self.hf_repo}/resolve/"
                f"{revision}/{self.hf_path}")

    def url(self, source: Source | None = None) -> str:
        src = source or self.source
        if src == Source.DIRECT:
            return self.direct_url
        if src == Source.MODELSCOPE:
            return self.modelscope_url()
        return self.hf_url()


# 许可提醒：R2T2 用的是网易自定义协议，不是 Apache 2.0。
# 协议第 3.4b 条要求每个副本保留协议全文与版权声明，故下载时要一并落盘。
R2T2_NOTE = ("NetEase Youdao 自定义模型协议（非 Apache 2.0）。"
             "协议全文见仓库 MODEL_LICENSE-R2T2.md；"
             "使用前须阅读 THIRD_PARTY_NOTICES.md。")

ASSETS: tuple[Asset, ...] = (
    Asset(
        key="asr_model",
        label="语音识别主模型（R2T2 Q4_K_M）",
        repo="netease-youdao/Confucius4-R2T2-GGUF",
        path="Confucius4-R2T2-Q4_K_M.gguf",
        size_bytes=1107404736,
        sha256="fa3cb46c8c3a66a58812b9098ba6e96a0266d4e8c9b3cf5ba34432fd2f9f6466",
        hf_repo="netease-youdao/Confucius4-R2T2-GGUF",
        hf_path="Confucius4-R2T2-Q4_K_M.gguf",
        license_note=R2T2_NOTE,
        purpose="把语音转成文字",
    ),
    Asset(
        key="asr_mmproj",
        label="语音识别音频侧投影（mmproj Q8_0）",
        repo="netease-youdao/Confucius4-R2T2-GGUF",
        path="mmproj-Confucius4-R2T2-Q8_0.gguf",
        size_bytes=348336544,
        sha256="8dc2c67e6a0484114928142d098db7ad94ae9f34c78948ef9d37a9678418cb65",
        hf_repo="netease-youdao/Confucius4-R2T2-GGUF",
        hf_path="mmproj-Confucius4-R2T2-Q8_0.gguf",
        license_note=R2T2_NOTE,
        purpose="让识别模型能处理音频输入",
    ),
    Asset(
        key="mt_model",
        label="翻译模型（Hy-MT2 1.8B Q4_K_M）",
        # 大小写敏感：Hunyuan 的 H、y 都要大写
        repo="Tencent-Hunyuan/Hy-MT2-1.8B-GGUF",
        path="Hy-MT2-1.8B-Q4_K_M.gguf",
        size_bytes=1133080448,
        sha256="dc5f44fcf1fa496ee7ad725982c0c8c553a4de00259b53af84c4b89fb0c06699",
        hf_repo="tencent/Hy-MT2-1.8B-GGUF",
        hf_path="Hy-MT2-1.8B-Q4_K_M.gguf",
        license_note="Apache 2.0",
        purpose="把文字翻译成目标语言",
    ),
    Asset(
        key="vad",
        label="语音活动检测模型（Silero VAD v5.1.2）",
        # 实测 ModelScope 上没有这个仓库（AI-ModelScope / csukuangfj /
        # snakers4 三个候选都查不到该文件），只能按 URL 从 GitHub 取。
        repo="snakers4/silero-vad",
        path="src/silero_vad/data/silero_vad.onnx",
        size_bytes=2327524,
        # 必须锁定 v5.1.2 标签：master 分支是 v6.x，接口变了会**静默失效**
        # （在明显是人声的音频上概率只有 0.056，不报错，只是检出 0 段）
        sha256="2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f",
        hf_repo="snakers4/silero-vad",
        hf_path="src/silero_vad/data/silero_vad.onnx",
        source=Source.DIRECT,
        direct_url=("https://raw.githubusercontent.com/snakers4/silero-vad/"
                    "v5.1.2/src/silero_vad/data/silero_vad.onnx"),
        license_note="MIT",
        purpose="切出语音区间（能量法会被背景音乐骗）",
    ),
)

_BY_KEY = {a.key: a for a in ASSETS}


def get(key: str) -> Asset:
    try:
        return _BY_KEY[key]
    except KeyError:
        raise KeyError(f"未知权重：{key}；可用：{sorted(_BY_KEY)}") from None


def total_size_bytes() -> int:
    return sum(a.size_bytes for a in ASSETS)


# 落盘时的相对目录。**不能含空格** —— 含空格的绝对路径会让 llama-server
# 拒绝启动、让 ffmpeg 的字幕滤镜打不开文件。
def rel_path(asset: Asset) -> str:
    if asset.key in ("asr_model", "asr_mmproj"):
        return f"r2t2/{asset.path}"
    if asset.key == "mt_model":
        return f"hy-mt2/{asset.path}"
    return f"vad/{asset.path}"


# 网易协议全文：协议 3.4b 要求每个副本保留，落盘时一并写入。
LICENSE_FILES = {
    "R2T2": ("MODEL_LICENSE-R2T2.md",
             "https://raw.githubusercontent.com/netease-youdao/"
             "Confucius4-R2T2/refs/heads/master/MODEL_LICENSE"),
}
