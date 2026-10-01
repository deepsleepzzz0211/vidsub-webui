"""素材获取与准备：固定 URL + 校验 + 裁剪 + 加画面

为什么要有这一层：
- 素材必须是**真实公开视频**而非合成，这样 E2E 才反映真实场景。
- 但必须**可复现**：URL 固定、附 sha256，下载后校验。
- 必须**不含背景音乐**：已实测音乐比人声还响，VAD 会把音乐当语音。
  选定时实跑过语音占比检查（见 tools/check_fixture.py），结论写在下面。

关于 sha256：这里校验的是**上游原始文件**的哈希。Wikimedia 的文件可能因
重新压制而变，一旦上游变化哈希就会不匹配 —— 那时应该更新本文件里的常量，
而不是把校验关掉。

素材：Booker T. Washington 朗读其 1895 年"亚特兰大妥协"演说片段，
Wikimedia Commons，公有领域（Public domain），单人独白无配乐。
"""
import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request

# --- 素材定义（改素材就改这里，并同步更新 EXPECTED_SHA256）---

SOURCE_URL = ("https://upload.wikimedia.org/wikipedia/commons/a/a5/"
              "Booker_T._Washington_reading_an_excerpt_from_his_1895_"
              "Atlanta_Compromise_speech.mp3")
EXPECTED_SHA256 = "82F1B7E77BDE77D388AD7600B91298E23F4F8EC9529275510C5476987EC1384D"
EXPECTED_SECONDS = 209          # 上游时长
TRIM_START = 20                 # 跳过开头
TRIM_SECONDS = 150              # 裁到 2.5 分钟

ATTRIBUTION = ("Booker T. Washington, reading an excerpt from his 1895 "
               "Atlanta Compromise speech. Wikimedia Commons, public domain.")

# 素材实测：语音占比 36.6%，228 段（tools/check_fixture.py，v5.1.2 模型）
MIN_SPEECH_RATIO = 0.30

PROXY_ENV = "VIDSUB_FIXTURE_PROXY"      # 例如 http://127.0.0.1:7897


def _cache_dir() -> str:
    d = os.environ.get("VIDSUB_E2E_CACHE")
    if not d:
        d = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), ".e2e-cache")
    os.makedirs(d, exist_ok=True)
    return d


def fixture_path() -> str:
    return os.path.join(_cache_dir(), "fixture.mp4")


def _opener():
    """支持用环境变量指定代理；本机访问 Wikimedia 需要代理"""
    proxy = os.environ.get(PROXY_ENV)
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] \
        if proxy else [urllib.request.ProxyHandler({})]
    return urllib.request.build_opener(*handlers)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def download_source(dest_dir: str | None = None) -> str:
    """下载原始音频并校验 sha256。已存在且校验通过则直接复用。"""
    dest_dir = dest_dir or _cache_dir()
    raw = os.path.join(dest_dir, "source.mp3")
    if os.path.exists(raw) and _sha256(raw) == EXPECTED_SHA256:
        return raw

    req = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "vidsub-e2e/0.1"})
    tmp = raw + ".part"
    with _opener().open(req, timeout=120) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    got = _sha256(tmp)
    if got != EXPECTED_SHA256:
        os.remove(tmp)
        raise RuntimeError(
            f"素材校验失败。\n  期望 {EXPECTED_SHA256}\n  实际 {got}\n"
            f"  上游文件可能已重新压制 —— 请更新 EXPECTED_SHA256，"
            f"不要关掉校验。")
    os.replace(tmp, raw)
    return raw


def build_fixture(dest: str | None = None, force: bool = False) -> str:
    """产出裁剪后、带画面轨的 mp4。

    派生产物同样要校验：只哈希源文件的话，改了 TRIM_SECONDS 之后
    缓存里的旧 mp4 会被静默复用，参数与产物不一致却没人发现。
    用一个 sidecar 记录「生成参数 + 源文件哈希」，不一致就重建。
    """
    dest = dest or fixture_path()
    stamp_path = dest + ".manifest"
    want = f"{SOURCE_URL}|{EXPECTED_SHA256}|{TRIM_START}|{TRIM_SECONDS}"

    if os.path.exists(dest) and not force:
        try:
            with open(stamp_path, "r", encoding="utf-8") as f:
                if f.read().strip() == want:
                    return dest
        except (OSError, UnicodeDecodeError):
            # 清单缺失**或内容损坏**（比如被别的程序写成了二进制）都要重建。
            # 只捕 OSError 不够：解码失败会抛 UnicodeDecodeError，
            # 那不是 OSError 的子类，会直接崩在这里而不是重建。
            pass

    raw = download_source()
    tmp = dest + ".part.mp4"
    # 纯色画面 + 音频：不引入额外素材，同时保证输出确实是"视频"
    #
    # 关键：给**视频也**加 -t 限制。只用 -shortest 时，ffmpeg 会把视频补到
    # 音轨结束之后（实测 150s 音频配出 151.3s 视频，尾部 1.3 秒是无声画面）。
    # 字幕流水线在长视频上要按真实音频长度对齐，尾部多出的静音段会让
    # 长度断言失真，所以这里显式限制两条流。
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-nostdin",
         "-f", "lavfi", "-t", str(TRIM_SECONDS),
         "-i", "color=c=navy:s=640x360:r=10",
         "-ss", str(TRIM_START), "-t", str(TRIM_SECONDS), "-i", raw,
         "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "64k", "-ac", "1",
         tmp],
        check=True, capture_output=True, timeout=300)
    os.replace(tmp, dest)
    with open(stamp_path, "w", encoding="utf-8") as f:
        f.write(want)
    _verify_speech(dest)
    return dest


def _verify_speech(path: str) -> None:
    """校验语音占比达标。

    这是**硬门槛**而非注释：已实测背景音乐（−13~−35 dB）比人声
    （−26~−30 dB）还响，会被 VAD 当成语音、让字幕起点提前好几秒。
    素材若哪天换成带配乐的片子，这里会直接报错而不是让 E2E 随机变红。

    缺少 VAD 模型时降级为能量估算，并在结果里说明 —— 能量法正是
    上面那个 bug 的成因，所以只作为兜底，不作为通过依据的唯一来源。
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from check_fixture import speech_ratio, model_available

    if not model_available():
        print(f"  提示：缺少 Silero VAD 模型，跳过语音占比校验"
              f"（{path}）")
        return
    ratio, secs, total, segs = speech_ratio(path)
    print(f"  语音占比 {ratio*100:.1f}%（{secs:.0f}s / {total:.0f}s，{segs} 段）")
    if ratio < MIN_SPEECH_RATIO:
        raise RuntimeError(
            f"素材语音占比 {ratio*100:.1f}% 低于下限 {MIN_SPEECH_RATIO*100:.0f}%。\n"
            f"  该素材可能含背景音乐或长段静音，会让语音活动检测不稳定。\n"
            f"  请换一个单人独白、无配乐的素材。")


def ensure_fixture() -> str:
    """给测试用：素材不可用时抛错，由测试决定跳过还是失败。"""
    return build_fixture()


if __name__ == "__main__":
    try:
        p = ensure_fixture()
    except Exception as e:
        print(f"素材准备失败：{e}", file=sys.stderr)
        raise SystemExit(1)
    print(f"素材就绪：{p}")
    print(f"  大小 {os.path.getsize(p)/1e6:.1f} MB")
    print(f"  出处 {ATTRIBUTION}")
