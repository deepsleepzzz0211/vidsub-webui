"""SSE 进度流

要验：作业进度变化时推出新的 data:，作业到终态（done/failed 且没还在压制）
时收尾退出，不跟踪错误作业。
"""
import json

from vidsub import server


class _Fake:
    def __init__(self, steps):
        self._steps = list(steps)
        self._i = 0

    def get(self, _id):
        step = self._steps[min(self._i, len(self._steps) - 1)]
        if self._i < len(self._steps) - 1:
            self._i += 1
        return step


def _job(state, label="", error="", burn=""):
    return {"id": "x", "state": state, "error": error,
            "progress": {"i": 0, "total": 10, "label": label},
            "burn_state": burn}


def test_stream_emits_each_state_change(monkeypatch):
    fake = _Fake([_job("running", "切分"), _job("running", "识别 3/10"), _job("done", "完成")])
    monkeypatch.setattr(server, "jobs", lambda: fake)
    events = []
    for chunk in server.job_event_stream("x", max_ticks=10, pause=0):
        if chunk.startswith("data: "):
            events.append(json.loads(chunk[6:]))
    states = [e["state"] for e in events]
    assert states == ["running", "running", "done"], states
    labels = [e["progress"]["label"] for e in events]
    assert labels == ["切分", "识别 3/10", "完成"]


def test_stream_terminates_at_done(monkeypatch):
    """done 且没在压制 → 流应收尾，不能一直 polling 到 max_ticks。"""
    fake = _Fake([_job("done", "完成")])
    monkeypatch.setattr(server, "jobs", lambda: fake)
    events = list(server.job_event_stream("x", max_ticks=50, pause=0))
    assert len(events) == 1, f"done 之后还在推：{events}"


def test_stream_keeps_open_while_burn_pending(monkeypatch):
    """字幕已出但仍在压 → 不是终态，要继续推进度。"""
    fake = _Fake([_job("done", "完成", burn="running"), _job("done", "完成", burn="done")])
    monkeypatch.setattr(server, "jobs", lambda: fake)
    events = [json.loads(c[6:]) for c in
              server.job_event_stream("x", max_ticks=10, pause=0)
              if c.startswith("data: ")]
    assert events[-1]["burn_state"] == "done"
    assert all(e["state"] == "done" for e in events)


def test_stream_reports_error_state(monkeypatch):
    fake = _Fake([_job("running", "识别"), _job("failed", "失败", error="VAD 没检测到语音")])
    monkeypatch.setattr(server, "jobs", lambda: fake)
    events = [json.loads(c[6:]) for c in
              server.job_event_stream("x", max_ticks=10, pause=0)
              if c.startswith("data: ")]
    assert events[-1]["state"] == "failed"
    assert "没检测到语音" in events[-1]["error"]


def test_stream_ends_when_job_vanishes(monkeypatch):
    class EmptyJobs:
        def get(self, _id):
            return None
    monkeypatch.setattr(server, "jobs", lambda: EmptyJobs())
    events = list(server.job_event_stream("x", max_ticks=10, pause=0))
    assert events == ["event: error\ndata: 作业不存在\n\n"]


def test_stream_survives_long_job(monkeypatch):
    """长任务不能被上限掐断。

    05 号票的前提是「70 分钟视频跑 20 分钟以上」。曾经 max_ticks=600
    （10 分钟）会把连接在作业跑到一半时掐掉，页面从此不再更新 ——
    这条钉死默认上限必须远大于任何真实作业时长。
    """
    assert server.SSE_MAX_TICKS >= 3600, \
        f"上限 {server.SSE_MAX_TICKS}s 太短，长任务会被中途断流"

    # 一个 5 小时还在 running 的作业：流不能提前结束
    class StillRunning:
        def get(self, _id):
            return _job("running", "识别 3/226")
    monkeypatch.setattr(server, "jobs", lambda: StillRunning())
    got = list(server.job_event_stream("x", max_ticks=30, pause=0,
                                       heartbeat_every=0))
    # 30 tick 只推 1 条 data（状态没变），但绝不能收尾
    assert len(got) == 1, got


def test_stream_heartbeats_while_quiet(monkeypatch):
    """状态长时间不变也要发心跳，否则反向代理会把连接当空闲掐掉。"""
    class StillRunning:
        def get(self, _id):
            return _job("running", "识别 3/226")
    monkeypatch.setattr(server, "jobs", lambda: StillRunning())
    got = list(server.job_event_stream("x", max_ticks=8, pause=0,
                                       heartbeat_every=3))
    assert sum(1 for c in got if c.startswith(": keepalive")) == 2, got