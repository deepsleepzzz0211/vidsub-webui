"""上传 → 作业 → 产物（04 号票的服务端接口）

要验的行为：
- 上传一个合法媒体文件，页面能从 probe 拿到时长/分辨率/体积/音轨
- 建 job 后后台跑，状态机 pending→running→done，产物在持久目录
- 没有音轨的文件在源头被拒（422），不产空字幕
- 缺模型时拒建 job（409）
- 损坏/非媒体文件报 422 且有可读原因

替身：把 pipeline.run 换成一个假的（写个假 SRT 就算成功），避免依赖
真实的 ASR/MT 与 llama-server。
"""
import io
import os
import wave

import pytest
from fastapi.testclient import TestClient

from vidsub import jobs, pipeline, registry, server


def _fake_bytes(key="x") -> bytes:
    seed = (key + "-weight-bytes-").encode()
    return (seed * (2048 // len(seed) + 1))[:2048]


def _shrink(monkeypatch):
    import hashlib
    size = 2048
    small = [registry.Asset(
        key=a.key, label=a.label, repo=a.repo, path=a.path, size_bytes=size,
        sha256=hashlib.sha256(_fake_bytes(a.key)).hexdigest(),
        hf_repo=a.hf_repo, hf_path=a.hf_path, source=a.source,
        license_note=a.license_note, purpose=a.purpose)
        for a in registry.ASSETS]
    monkeypatch.setattr(registry, "ASSETS", tuple(small))


def _seed_models():
    root = server.data_dir()
    for a in registry.ASSETS:
        p = os.path.join(root, *registry.rel_path(a).split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(_fake_bytes(a.key))


def _wav_bytes(seconds=1.0) -> bytes:
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(16000)
    w.writeframes(b"\x00\x00" * int(16000 * seconds))
    w.close()
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _setup(monkeypatch, tmp_path):
    _shrink(monkeypatch)
    monkeypatch.setenv("VIDSUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("vidsub.server._jobs", None)
    server._rt = None
    server._rt_root = ""
    yield
    server._rt = None
    server._rt_root = ""
    monkeypatch.setattr("vidsub.server._jobs", None)


@pytest.fixture
def client():
    with TestClient(server.app) as c:
        yield c


def _patch_pipeline(monkeypatch):
    def _fake_run(video, out_srt, work_dir=None, progress=None,
                  on_stage=None, **kw):
        if on_stage:
            on_stage("抽取音轨")
            on_stage("切分语音")
        if progress:
            progress(1, 3, "识别并翻译", 1)
            progress(3, 3, "识别并翻译", 3)
        if on_stage:
            on_stage("写出字幕")
        os.makedirs(os.path.dirname(out_srt), exist_ok=True)
        with open(out_srt, "w", encoding="utf-8") as f:
            f.write("1\n00:00:00,000 --> 00:00:01,000\n你好\nhello\n\n")
        return out_srt

    monkeypatch.setattr(jobs.pipeline, "run", _fake_run)
    # 跳过真运行时，直接起作业
    monkeypatch.setattr("vidsub.server._ensure_runtime_then_start",
                        _start_directly)


def _wait_state(job_id: str, want: str, timeout: float = 5.0) -> dict:
    import time
    deadline = time.time() + timeout
    jm = server.jobs().get(job_id)
    while time.time() < deadline and jm and jm["state"] != want:
        time.sleep(0.05)
        jm = server.jobs().get(job_id)
    return jm


def _start_directly(job_id):
    jm = server.jobs()
    jm.start(job_id)


# --- 缺模型时拒建 ----------------------------------------------------------

def test_refuses_upload_when_models_missing(client):
    r = client.post("/api/jobs", files={"file": ("a.wav", _wav_bytes(), "audio/wav")})
    assert r.status_code == 409
    assert "下载" in r.json()["error"]


# --- 合法上传 ----------------------------------------------------------

def test_valid_upload_creates_and_finishes_job(client, monkeypatch):
    _seed_models()
    _patch_pipeline(monkeypatch)
    r = client.post("/api/jobs", files={"file": ("a.wav", _wav_bytes(), "audio/wav")})
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["state"] in ("pending", "running") or job["state"] == "done"
    assert job["info"]["has_audio"] is True

    # 等 job 跑完
    import time
    for _ in range(50):
        jm = server.jobs().get(job["id"])
        if jm["state"] == "done":
            break
        time.sleep(0.1)
    jm = server.jobs().get(job["id"])
    assert jm["state"] == "done", jm["error"]

    srt = client.get(f"/api/jobs/{job['id']}/srt")
    assert srt.status_code == 200
    assert "你好" in srt.text


def test_upload_probe_populates_media_info(client, monkeypatch):
    _seed_models()
    _patch_pipeline(monkeypatch)
    body = _wav_bytes()
    r = client.post("/api/jobs", files={"file": ("a.wav", body, "audio/wav")})
    job = r.json()
    assert job["info"]["duration"] == pytest.approx(1.0, abs=0.5)
    # 体积要等于**实际上传的字节数**：页面上那个"体积"得和用户在文件管理器
    # 里看到的一致，不能是 ffprobe 按码率估出来的值。
    #
    # （这行以前写的是 `== r.request.stream if False else True` —— 三元条件
    # 恒真，断言从来没执行过。`r.request.stream` 还是个流对象、根本不是尺寸。）
    assert job["info"]["size_bytes"] == len(body)
    assert job["info"]["has_audio"] is True


def test_non_media_upload_is_rejected(client, monkeypatch):
    _seed_models()
    _patch_pipeline(monkeypatch)
    r = client.post("/api/jobs", files={"file": ("a.txt", b"not a video", "text/plain")})
    assert r.status_code == 422
    assert "无法解析" in r.json()["error"]


def test_missing_file_name(client, monkeypatch):
    _seed_models()
    r = client.post("/api/jobs", files={"file": ("", _wav_bytes(), "audio/wav")})
    # 空文件名或非法上传：400/422 都算被挡下，不进入建 job 流程
    assert r.status_code in (400, 422)


def test_job_list_newest_first(client, monkeypatch):
    _seed_models()
    _patch_pipeline(monkeypatch)
    client.post("/api/jobs", files={"file": ("a.wav", _wav_bytes(), "audio/wav")})
    client.post("/api/jobs", files={"file": ("b.wav", _wav_bytes(), "audio/wav")})
    jobs_list = client.get("/api/jobs").json()["jobs"]
    assert len(jobs_list) == 2
    assert jobs_list[0]["created_at"] >= jobs_list[1]["created_at"]


def test_unknown_job_returns_404(client):
    r = client.get("/api/jobs/nope")
    assert r.status_code == 404


def test_job_directory_has_no_spaces(client, monkeypatch):
    """产物目录不能含空格 —— 否则后面的 ffmpeg/llama-server 一律失败"""
    _seed_models()
    _patch_pipeline(monkeypatch)
    client.post("/api/jobs", files={"file": ("a.wav", _wav_bytes(), "audio/wav")})
    b = client.get("/api/jobs").json()["jobs"]
    jm = server.jobs()
    for j in b:
        jmj = jm.get(j["id"])
        assert jmj is not None, f"job {j['id']} 在管理器里找不到"
        assert " " not in jmj["srt"]
        assert " " not in jmj["video"]


# --- 分阶段进度（05）--------------------------------------------------------

def test_progress_carries_translated_count(client, monkeypatch):
    """进度里要带「已翻译 N 条」这个计数，页面才能显示它（05）。"""
    seen: list = []

    def _fake_run(video, out_srt, work_dir=None, progress=None,
                  on_stage=None, **kw):
        if on_stage:
            on_stage("抽取音轨")
            on_stage("切分语音")
        if progress:
            progress(2, 5, "识别并翻译", 2)
        os.makedirs(os.path.dirname(out_srt), exist_ok=True)
        with open(out_srt, "w", encoding="utf-8") as f:
            f.write("1\n00:00:00,000 --> 00:00:01,000\n你好\nhello\n\n")
        return out_srt

    monkeypatch.setattr(jobs.pipeline, "run", _fake_run)
    monkeypatch.setattr("vidsub.server._ensure_runtime_then_start",
                        _start_directly)

    stages: list = []
    real_update = jobs.JobManager.update

    def _spy(self, job_id, **fields):
        p = fields.get("progress")
        if p:
            if p.get("translated"):
                seen.append(p["translated"])
            if p.get("label"):
                stages.append(p["label"])
        return real_update(self, job_id, **fields)

    monkeypatch.setattr(jobs.JobManager, "update", _spy)

    _seed_models()
    job = client.post("/api/jobs",
                      files={"file": ("a.wav", _wav_bytes(), "audio/wav")}).json()
    jm = _wait_state(job["id"], "done")
    assert jm["state"] == "done", jm["error"]
    assert seen, "进度里没有 translated 计数，页面显示不了「已翻译 N 条」"
    # 大阶段（抽音轨/切分）必须出现，否则长视频前几分钟看着像卡死
    assert any("抽取音轨" in s for s in stages), stages
    assert any("切分语音" in s for s in stages), stages


def test_job_holds_runtime_reference_while_running(client, monkeypatch):
    """作业跑着的时候必须持有 runtime 引用计数（08）。

    否则 09 号票那种 20 分钟的作业，会被空闲巡检在跑到一半时把模型
    杀掉，后半程直接连不上推理服务。
    """
    _seed_models()
    _patch_pipeline(monkeypatch)

    import vidsub.runtime as rt_mod
    seen: list = []
    real_acquire = rt_mod.Runtime.acquire
    real_release = rt_mod.Runtime.release

    monkeypatch.setattr(rt_mod.Runtime, "acquire",
                        lambda self: (seen.append("acquire"),
                                      real_acquire(self))[1])
    monkeypatch.setattr(rt_mod.Runtime, "release",
                        lambda self: (seen.append("release"),
                                      real_release(self))[1])

    job = client.post("/api/jobs",
                      files={"file": ("a.wav", _wav_bytes(), "audio/wav")}).json()
    jm = _wait_state(job["id"], "done")
    assert jm["state"] == "done", jm["error"]
    assert "acquire" in seen, "作业运行时没有登记引用计数"
    assert "release" in seen, "作业结束没有释放引用计数（服务会被永久钉住）"


def test_intermediates_cleaned_after_success(client, monkeypatch):
    """成功后中间品要清掉，产物要留着。

    12 号票要求"素材用完即清理，不占满磁盘"（70 分钟视频会切几百个
    seg_*.wav）；08 号票要求"产物不自动删"。删中间品、留产物，两者同时满足。
    """
    _seed_models()

    def _fake_run(video, out_srt, work_dir=None, progress=None,
                  on_stage=None, **kw):
        os.makedirs(work_dir, exist_ok=True)
        for name in ("audio.wav", "seg_0000.wav", "seg_0001.wav"):
            with open(os.path.join(work_dir, name), "wb") as f:
                f.write(b"x" * 32)
        os.makedirs(os.path.dirname(out_srt), exist_ok=True)
        with open(out_srt, "w", encoding="utf-8") as f:
            f.write("1\n00:00:00,000 --> 00:00:01,000\n你好\nhello\n\n")
        return out_srt

    monkeypatch.setattr(jobs.pipeline, "run", _fake_run)
    monkeypatch.setattr("vidsub.server._ensure_runtime_then_start",
                        _start_directly)

    job = client.post("/api/jobs",
                      files={"file": ("a.wav", _wav_bytes(), "audio/wav")}).json()
    jm = _wait_state(job["id"], "done")
    assert jm["state"] == "done", jm["error"]

    work = jm["work_dir"]
    assert not os.path.exists(os.path.join(work, "audio.wav")), "audio.wav 没清掉"
    assert not os.path.exists(os.path.join(work, "seg_0000.wav")), "seg_*.wav 没清掉"
    # 产物必须还在
    assert os.path.isfile(jm["srt"]), "字幕被误删了"
    assert os.path.isfile(jm["video"]), "上传的源视频被误删了"


def test_done_is_published_only_after_cleanup(client, monkeypatch):
    """发布 `done` 的那一刻，清理必须已经完成。

    `done` 是给外界的完成信号：页面轮询、SSE、E2E 断言看到它就会去读工作区。
    先发布再清理会留下一个窗口 —— 全量测试里真撞上过：轮询到 done 立刻断言
    中间品已删，而清理还没跑完。症状是**单独跑通过、全量跑失败**，最难查的
    那一类 flaky。

    所以这里不看"清理有没有发生"，而看**它发生的时候作业是什么状态**。
    """
    _seed_models()
    _patch_pipeline(monkeypatch)

    seen = []
    real = jobs.JobManager._clean_work

    def _spy(self, job_id):
        jm = self.get(job_id)
        seen.append(jm["state"] if jm else None)
        return real(self, job_id)

    monkeypatch.setattr(jobs.JobManager, "_clean_work", _spy)

    job = client.post("/api/jobs",
                      files={"file": ("a.wav", _wav_bytes(), "audio/wav")}).json()
    jm = _wait_state(job["id"], "done")
    assert jm["state"] == "done", jm["error"]

    assert seen, "清理根本没被调用"
    assert all(s != "done" for s in seen), (
        f"清理被调用时作业已经是 done 了（状态序列 {seen}）—— "
        f"客户端会在这个窗口里读到没清干净的工作区")


def test_intermediates_kept_after_failure(client, monkeypatch):
    """失败的作业要留下中间品 —— 那是排查现场。"""
    _seed_models()

    def _boom(video, out_srt, work_dir=None, progress=None,
              on_stage=None, **kw):
        os.makedirs(work_dir, exist_ok=True)
        with open(os.path.join(work_dir, "audio.wav"), "wb") as f:
            f.write(b"x" * 32)
        raise pipeline.PipelineError("推理服务超时")

    monkeypatch.setattr(jobs.pipeline, "run", _boom)
    monkeypatch.setattr("vidsub.server._ensure_runtime_then_start",
                        _start_directly)

    job = client.post("/api/jobs",
                      files={"file": ("a.wav", _wav_bytes(), "audio/wav")}).json()
    jm = _wait_state(job["id"], "failed")
    assert jm["state"] == "failed"
    assert os.path.isfile(os.path.join(jm["work_dir"], "audio.wav")), \
        "失败的作业把现场清掉了，没法排查"


# --- 压制成片（06） ---------------------------------------------------------

def test_burn_stage_files_cleaned_but_output_kept(client, monkeypatch, tmp_path):
    """压制完，暂存目录里的 in.*/sub*.srt 要清掉，成片要留着。

    burn 会把源视频和字幕复制进暂存目录再跑 ffmpeg —— 70 分钟视频那就是
    两份几百 MB 的复制品。不清就等于每个作业白占一份源视频大小。
    """
    import subprocess
    from vidsub import audio as _a

    _seed_models()
    _patch_pipeline(monkeypatch)

    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc=duration=2:size=320x240:rate=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(src)],
        check=True, capture_output=True)

    job = client.post("/api/jobs", files={
        "file": ("src.mp4", src.read_bytes(), "video/mp4")}).json()
    jm = _wait_state(job["id"], "done", timeout=20.0)
    assert jm["state"] == "done", jm["error"]

    r = client.post(f"/api/jobs/{job['id']}/burn")
    assert r.status_code == 200, r.text

    import time
    deadline = time.time() + 120
    while time.time() < deadline:
        cur = server.jobs().get(job["id"])
        if cur["burn_state"] in ("done", "failed"):
            break
        time.sleep(0.3)
    cur = server.jobs().get(job["id"])
    assert cur["burn_state"] == "done", cur.get("error")

    work = cur["work_dir"]
    leftovers = [n for n in os.listdir(work)
                 if n == "audio.wav" or n.startswith(("seg_", "in.", "sub.srt",
                                                     "sub_zh.srt"))]
    assert leftovers == [], f"暂存目录还留着中间品：{leftovers}"
    assert os.path.isfile(cur["video_mp4"]), "成片被误删了"
    assert _a.has_audio(cur["video_mp4"]), "成片没有音轨"


def test_burn_endpoint_produces_playable_video(client, monkeypatch, tmp_path):
    """POST /api/jobs/{id}/burn 起压制，完成后 video 接口能拿到成片"""
    import subprocess
    from vidsub import audio as _a

    _seed_models()
    _patch_pipeline(monkeypatch)

    # 造一个带音轨的真视频
    vp = os.path.join(str(tmp_path), "v.mp4")
    r = subprocess.run([
        _a.ffmpeg(), "-y", "-loglevel", "error", "-nostdin",
        "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=15",
        "-f", "lavfi", "-i", "sine=frequency=220:duration=1",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", vp], capture_output=True, text=True)
    assert os.path.exists(vp), r.stderr

    r = client.post("/api/jobs", files={"file": ("v.mp4", open(vp, "rb"), "video/mp4")})
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]

    # 等 SRT job 完成
    import time
    for _ in range(50):
        if server.jobs().get(job_id)["state"] == "done":
            break
        time.sleep(0.1)
    assert server.jobs().get(job_id)["state"] == "done"

    r = client.post(f"/api/jobs/{job_id}/burn")
    assert r.status_code == 200, r.text

    for _ in range(100):
        bs = server.jobs().get(job_id).get("burn_state")
        if bs in ("done", "failed"):
            break
        time.sleep(0.2)
    jm = server.jobs().get(job_id)
    assert jm["burn_state"] == "done", jm.get("error")
    assert os.path.exists(jm["video_mp4"])

    vid = client.get(f"/api/jobs/{job_id}/video")
    assert vid.status_code == 200


def test_burn_endpoint_rejects_when_srt_not_ready(client):
    """不存在的 job 不能压"""
    r = client.post("/api/jobs/nonexistent/burn")
    # start_burn 抛 JobError → 409
    assert r.status_code == 409
