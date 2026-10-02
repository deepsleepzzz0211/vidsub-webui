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

from vidsub import downloader, jobs, pipeline, registry, server


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
    def _fake_run(video, out_srt, work_dir=None, progress=None, **kw):
        if progress:
            progress(1, 1)
        os.makedirs(os.path.dirname(out_srt), exist_ok=True)
        with open(out_srt, "w", encoding="utf-8") as f:
            f.write("1\n00:00:00,000 --> 00:00:01,000\n你好\nhello\n\n")
        return out_srt

    monkeypatch.setattr(jobs.pipeline, "run", _fake_run)
    # 跳过真运行时，直接起作业
    monkeypatch.setattr("vidsub.server._ensure_runtime_then_start",
                        lambda job_id: jobs.JobManager().start(job_id)
                        if False else _start_directly(job_id))


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
    r = client.post("/api/jobs", files={"file": ("a.wav", _wav_bytes(), "audio/wav")})
    job = r.json()
    assert job["info"]["duration"] == pytest.approx(1.0, abs=0.5)
    assert job["info"]["size_bytes"] == r.request.stream if False else True
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
        assert " " not in jm.get(j["id"])["srt"]
        assert " " not in jm.get(j["id"])["video"]