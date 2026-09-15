from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from remote_host_mcp.hub_settings import HubSettings
from remote_host_mcp.report_agent import (
    HubClient,
    HubRejected,
    HubUnavailable,
    ReportAgent,
    AuditTail,
    prepare_event,
)


def _event(**overrides: object) -> dict:
    base = {
        "id": "cal_abcdef123456",
        "provider": "rhmcp",
        "toolName": "exec",
        "status": "ok",
        "startedAt": "2026-09-15T10:03:12.345Z",
        "durationMs": 12,
        "argsText": '{"command": "ls -la"}',
        "outputBytes": 5,
        "outputPreview": "hello",
        "outputRef": None,
    }
    base.update(overrides)
    return base


def _write_log(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._body = json.dumps(payload).encode()
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_: object) -> bool:
        return False


class _RecordingOpener:
    """Stands in for urlopen; records requests and can be scripted to fail.

    ``fail_on`` maps a URL suffix to the exception to raise for it, so a test can
    fail one endpoint (say the report POST) without disturbing the heartbeat.
    """

    def __init__(
        self,
        responses: list[object] | None = None,
        fail_on: dict[str, BaseException] | None = None,
    ) -> None:
        self.requests: list[tuple[str, dict, dict]] = []
        self.responses = list(responses or [])
        self.fail_on = dict(fail_on or {})
        self.default = {"ok": True, "accepted": 0, "rejected": 0}

    def __call__(self, request: object, timeout: float | None = None) -> _FakeResponse:
        url = request.full_url  # type: ignore[attr-defined]
        body = json.loads(request.data.decode()) if request.data else None  # type: ignore[attr-defined]
        self.requests.append((url, dict(request.headers), body))  # type: ignore[attr-defined]
        for suffix, error in self.fail_on.items():
            if url.endswith(suffix):
                raise error
        if self.responses:
            nxt = self.responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return _FakeResponse(nxt)  # type: ignore[arg-type]
        return _FakeResponse(self.default)


def _reports(opener: _RecordingOpener) -> list[tuple[str, dict, dict]]:
    """Only the /agent/v1/report calls; heartbeat traffic is asserted separately."""
    return [call for call in opener.requests if call[0].endswith("/agent/v1/report")]


def _settings(**overrides: object) -> HubSettings:
    base: dict = {
        "base_url": "https://hub.example.com",
        "host_id": "host_abc",
        "agent_key": "agt_test",
        "report_level": "full",
        "redact_enabled": True,
        "report_interval_seconds": 30,
        "report_batch_size": 3,
        "report_max_batch": 200,
    }
    base.update(overrides)
    return HubSettings(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------- report levels


def test_full_level_keeps_arguments_and_output() -> None:
    prepared = prepare_event(_event(), report_level="full")
    assert prepared is not None
    assert "argsText" in prepared
    assert "outputPreview" in prepared


def test_tool_level_drops_output_but_keeps_arguments() -> None:
    prepared = prepare_event(_event(), report_level="tool")
    assert prepared is not None
    assert "argsText" in prepared
    assert "outputPreview" not in prepared
    assert "outputBytes" not in prepared


def test_meta_level_drops_arguments_and_output() -> None:
    prepared = prepare_event(_event(), report_level="meta")
    assert prepared is not None
    assert "argsText" not in prepared
    assert "outputPreview" not in prepared
    assert prepared["toolName"] == "exec"


def test_events_missing_required_fields_are_skipped() -> None:
    assert prepare_event({"toolName": "exec", "status": "ok"}) is None
    assert prepare_event({"status": "ok", "startedAt": "t"}) is None


def test_prepare_event_redacts_by_default() -> None:
    prepared = prepare_event(_event(outputPreview="API_KEY=hunter2hunter2"))
    assert prepared is not None
    assert prepared["outputPreview"] == "API_KEY=[REDACTED]"


def test_prepare_event_honours_the_redaction_switch() -> None:
    prepared = prepare_event(_event(outputPreview="API_KEY=hunter2hunter2"), redact_enabled=False)
    assert prepared is not None
    assert prepared["outputPreview"] == "API_KEY=hunter2hunter2"


# ------------------------------------------------------------------ hub client


def test_report_sends_bearer_auth_and_returns_the_ack() -> None:
    opener = _RecordingOpener([{"ok": True, "accepted": 2, "rejected": 0}])
    client = HubClient("https://hub.example.com", "agt_test", opener=opener)
    ack = client.report([_event(), _event()])
    assert (ack.ok, ack.accepted, ack.rejected) == (True, 2, 0)

    url, headers, body = opener.requests[0]
    assert url == "https://hub.example.com/agent/v1/report"
    assert headers["Authorization"] == "Bearer agt_test"
    assert headers["Content-type"] == "application/json"
    assert len(body["events"]) == 2


def test_401_is_a_permanent_rejection() -> None:
    def opener(request: object, timeout: float | None = None) -> None:
        raise urllib.error.HTTPError(request.full_url, 401, "nope", {}, None)  # type: ignore[attr-defined]

    client = HubClient("https://hub.example.com", "agt_bad", opener=opener)
    with pytest.raises(HubRejected):
        client.report([_event()])


def test_5xx_and_network_errors_are_retryable() -> None:
    def server_error(request: object, timeout: float | None = None) -> None:
        raise urllib.error.HTTPError(request.full_url, 503, "down", {}, None)  # type: ignore[attr-defined]

    def offline(request: object, timeout: float | None = None) -> None:
        raise urllib.error.URLError("no route to host")

    with pytest.raises(HubUnavailable):
        HubClient("https://hub.example.com", "agt_test", opener=server_error).report([_event()])
    with pytest.raises(HubUnavailable):
        HubClient("https://hub.example.com", "agt_test", opener=offline).report([_event()])


# ------------------------------------------------------------------- tail/cursor


def test_tail_reads_only_new_lines_after_the_checkpoint(tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"
    cursor = tmp_path / "report.cursor"
    _write_log(log, [_event(toolName="a"), _event(toolName="b")])

    tail = AuditTail(log, cursor)
    first = list(tail.read_new())
    assert [event["toolName"] for _, event in first] == ["a", "b"]
    assert list(tail.read_new()) == []

    _write_log(log, [_event(toolName="c")])
    tail.commit(first[-1][0])
    assert [event["toolName"] for _, event in tail.read_new()] == ["c"]


def test_tail_restarts_when_the_log_is_truncated(tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"
    cursor = tmp_path / "report.cursor"
    _write_log(log, [_event(toolName="a")])
    tail = AuditTail(log, cursor)
    list(tail.read_new())
    tail.commit(999)  # pretend the checkpoint is ahead of a rotated file
    _write_log(log, [_event(toolName="fresh")])
    assert [event["toolName"] for _, event in tail.read_new()] == ["fresh"]


def test_tail_waits_for_a_complete_line(tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"
    log.write_text('{"toolName": "partial"', encoding="utf-8")
    tail = AuditTail(log, tmp_path / "report.cursor")
    assert list(tail.read_new()) == []
    _write_log(log, [])  # no-op, keeps the append semantics explicit
    with log.open("a", encoding="utf-8") as handle:
        handle.write(', "status": "ok", "startedAt": "t"}\n')
    assert [event["toolName"] for _, event in tail.read_new()] == ["partial"]


def test_cursor_survives_a_restart(tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"
    cursor = tmp_path / "report.cursor"
    _write_log(log, [_event(toolName="a")])
    tail = AuditTail(log, cursor)
    first = list(tail.read_new())
    tail.commit(first[-1][0])
    assert AuditTail(log, cursor).offset == first[-1][0]


# ------------------------------------------------------------------------ agent


def _agent(tmp_path: Path, opener: _RecordingOpener, **overrides: object) -> ReportAgent:
    log = tmp_path / "calls.jsonl"
    return ReportAgent(
        _settings(**overrides),
        HubClient("https://hub.example.com", "agt_test", opener=opener),
        log_path=log,
        cursor_path=tmp_path / "report.cursor",
    )


def test_batch_flushes_at_the_configured_size(tmp_path: Path) -> None:
    opener = _RecordingOpener()
    agent = _agent(tmp_path, opener)
    _write_log(agent.tail.log_path, [_event() for _ in range(3)])

    agent.cycle(now=1.0)

    assert len(_reports(opener)) == 1
    assert len(_reports(opener)[0][2]["events"]) == 3
    assert agent.pending_count == 0


def test_batch_flushes_on_the_interval_even_below_the_size(tmp_path: Path) -> None:
    opener = _RecordingOpener()
    agent = _agent(tmp_path, opener)
    _write_log(agent.tail.log_path, [_event()])

    agent.cycle(now=1.0)
    assert _reports(opener) == []  # not due yet

    agent.cycle(now=100.0)
    assert len(_reports(opener)) == 1


def test_batch_never_exceeds_the_per_request_cap(tmp_path: Path) -> None:
    opener = _RecordingOpener()
    agent = _agent(tmp_path, opener, report_max_batch=2, report_batch_size=1)
    _write_log(agent.tail.log_path, [_event() for _ in range(5)])

    agent.cycle(now=1.0)

    sizes = [len(request[2]["events"]) for request in _reports(opener)]
    assert sizes and all(size <= 2 for size in sizes)
    assert sum(sizes) == 5


def test_unacknowledged_batch_is_retried_and_not_lost(tmp_path: Path) -> None:
    # Only the report POST fails; the heartbeat still succeeds.
    opener = _RecordingOpener(fail_on={"/agent/v1/report": urllib.error.URLError("down")})
    agent = _agent(tmp_path, opener, report_batch_size=1)
    _write_log(agent.tail.log_path, [_event(toolName="must_not_be_lost")])

    agent.cycle(now=1.0)
    assert agent.pending_count == 1
    assert agent.tail.offset == 0  # nothing acknowledged, so nothing committed

    opener.fail_on.clear()
    agent.cycle(now=1000.0)  # past the backoff window
    assert len(_reports(opener)) == 2
    assert _reports(opener)[-1][2]["events"][0]["toolName"] == "must_not_be_lost"
    assert agent.pending_count == 0


def test_rejected_batch_does_not_block_the_queue(tmp_path: Path) -> None:
    def always_401(request: object, timeout: float | None = None) -> None:
        raise urllib.error.HTTPError(request.full_url, 401, "nope", {}, None)  # type: ignore[attr-defined]

    agent = _agent(tmp_path, _RecordingOpener(), report_batch_size=1)
    agent.client = HubClient("https://hub.example.com", "agt_bad", opener=always_401)
    _write_log(agent.tail.log_path, [_event(toolName="a"), _event(toolName="b")])

    agent.cycle(now=1.0)

    assert agent.pending_count == 0
    assert agent.tail.offset > 0


def test_malformed_lines_are_skipped_without_stalling(tmp_path: Path) -> None:
    opener = _RecordingOpener()
    agent = _agent(tmp_path, opener, report_batch_size=1)
    log = agent.tail.log_path
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("not json\n" + json.dumps({"toolName": "only_missing_fields"}) + "\n", encoding="utf-8")

    agent.cycle(now=1.0)

    assert _reports(opener) == []
    assert agent.pending_count == 0
    # The unparseable line is dropped by the tail; the incomplete event by the agent.
    assert agent.dropped == 1
    assert agent.tail.read_offset > 0


def test_backpressure_stops_reading_instead_of_dropping(tmp_path: Path) -> None:
    import remote_host_mcp.report_agent as module

    opener = _RecordingOpener(fail_on={"/agent/v1/report": urllib.error.URLError("down")})
    agent = _agent(tmp_path, opener, report_batch_size=1, report_interval_seconds=1)
    _write_log(agent.tail.log_path, [_event() for _ in range(4)])

    original = module.MAX_PENDING_EVENTS
    module.MAX_PENDING_EVENTS = 2
    try:
        agent.cycle(now=1.0)
        assert agent.pending_count == 2
        agent.cycle(now=100000.0)
        assert agent.pending_count <= 2
    finally:
        module.MAX_PENDING_EVENTS = original


def test_client_bugs_propagate_from_cycle_so_run_can_contain_them(tmp_path: Path) -> None:
    """`run()` is the guard that keeps a sidecar bug from killing the process."""

    def boom(request: object, timeout: float | None = None) -> None:
        raise RuntimeError("unexpected client bug")

    agent = _agent(tmp_path, _RecordingOpener(), report_batch_size=1)
    agent.client = HubClient("https://hub.example.com", "agt_test", opener=boom)
    _write_log(agent.tail.log_path, [_event()])
    with pytest.raises(RuntimeError):
        agent.cycle(now=1.0)


def test_build_agent_requires_full_hub_configuration(tmp_path: Path) -> None:
    from remote_host_mcp.hub_settings import HubConfigError
    from remote_host_mcp.report_agent import build_agent

    with pytest.raises(HubConfigError):
        build_agent(HubSettings())


# ------------------------------------------------------------ heartbeat/config


def _paths(opener: _RecordingOpener) -> list[str]:
    return [url.split("hub.example.com")[-1] for url, _, _ in opener.requests]


def test_heartbeat_posts_the_version_and_is_rate_limited(tmp_path: Path) -> None:
    opener = _RecordingOpener()
    agent = _agent(tmp_path, opener, heartbeat_seconds=60)

    agent.cycle(now=1.0)
    assert _paths(opener) == ["/agent/v1/heartbeat"]
    assert opener.requests[0][2] == {"version": __import__("remote_host_mcp").__version__}

    agent.cycle(now=10.0)  # inside the interval
    assert _paths(opener) == ["/agent/v1/heartbeat"]

    agent.cycle(now=100.0)
    assert _paths(opener).count("/agent/v1/heartbeat") == 2


def test_config_is_pulled_when_the_hub_reports_a_change(tmp_path: Path) -> None:
    opener = _RecordingOpener(
        [
            {"ok": True, "hostId": "host_abc", "configChanged": True},
            {"hostId": "host_abc", "reportLevel": "meta", "redactEnabled": False, "outputMode": "local"},
        ]
    )
    agent = _agent(tmp_path, opener)
    agent.cycle(now=1.0)

    assert _paths(opener) == ["/agent/v1/heartbeat", "/agent/v1/config"]
    assert agent.report_level == "meta"
    assert agent.redact_enabled is False
    assert agent.output_mode == "local"


def test_config_is_not_pulled_when_nothing_changed(tmp_path: Path) -> None:
    opener = _RecordingOpener([{"ok": True, "hostId": "host_abc", "configChanged": False}])
    agent = _agent(tmp_path, opener)
    agent.cycle(now=1.0)
    assert _paths(opener) == ["/agent/v1/heartbeat"]


def test_hub_redaction_switch_actually_changes_what_is_sent(tmp_path: Path) -> None:
    """The whole point of config pull: the switch has to reach the wire."""
    opener = _RecordingOpener()
    agent = _agent(tmp_path, opener, report_batch_size=1)
    assert agent.apply_config({"redactEnabled": False}) == ["redactEnabled True -> False"]

    _write_log(agent.tail.log_path, [_event(outputPreview="API_KEY=hunter2hunter2")])
    agent.cycle(now=1.0)

    sent = [r for r in opener.requests if r[0].endswith("/agent/v1/report")]
    assert sent and sent[0][2]["events"][0]["outputPreview"] == "API_KEY=hunter2hunter2"


def test_hub_report_level_change_reaches_the_wire(tmp_path: Path) -> None:
    opener = _RecordingOpener()
    agent = _agent(tmp_path, opener, report_batch_size=1)
    agent.apply_config({"reportLevel": "meta"})

    _write_log(agent.tail.log_path, [_event()])
    agent.cycle(now=1.0)

    sent = [r for r in opener.requests if r[0].endswith("/agent/v1/report")]
    assert sent
    assert "argsText" not in sent[0][2]["events"][0]
    assert "outputPreview" not in sent[0][2]["events"][0]


def test_invalid_config_values_are_ignored(tmp_path: Path) -> None:
    agent = _agent(tmp_path, _RecordingOpener())
    changes = agent.apply_config(
        {"reportLevel": "everything", "redactEnabled": "yes", "outputMode": "tape"}
    )
    assert changes == []
    assert agent.report_level == "full"
    assert agent.redact_enabled is True
    assert agent.output_mode == "preview"


def test_partial_config_leaves_other_settings_alone(tmp_path: Path) -> None:
    agent = _agent(tmp_path, _RecordingOpener())
    agent.apply_config({"redactEnabled": False})
    assert agent.report_level == "full"
    assert agent.output_mode == "preview"


def test_host_id_mismatch_is_surfaced_but_not_fatal(tmp_path: Path) -> None:
    agent = _agent(tmp_path, _RecordingOpener())
    agent.apply_config({"hostId": "host_other"})
    assert agent.settings.host_id == "host_abc"


def test_heartbeat_failure_is_retryable_and_does_not_stop_reporting(tmp_path: Path) -> None:
    opener = _RecordingOpener(fail_on={"/agent/v1/heartbeat": urllib.error.URLError("down")})
    agent = _agent(tmp_path, opener, report_batch_size=1)
    _write_log(agent.tail.log_path, [_event()])

    agent.cycle(now=1.0)

    assert _paths(opener)[0] == "/agent/v1/heartbeat"
    assert "/agent/v1/report" in _paths(opener)
    assert agent.pending_count == 0


def test_rejected_heartbeat_does_not_crash_the_agent(tmp_path: Path) -> None:
    def always_401(request: object, timeout: float | None = None) -> None:
        raise urllib.error.HTTPError(request.full_url, 401, "nope", {}, None)  # type: ignore[attr-defined]

    agent = _agent(tmp_path, _RecordingOpener())
    agent.client = HubClient("https://hub.example.com", "agt_bad", opener=always_401)
    assert agent.heartbeat(now=1.0) is False
    assert agent.next_heartbeat > 1.0


def test_run_pulls_config_before_the_first_cycle(tmp_path: Path) -> None:
    """A restart must adopt the Hub's current settings, not a stale rhmcp.env."""
    import remote_host_mcp.report_agent as module

    opener = _RecordingOpener(
        [
            {"hostId": "host_abc", "reportLevel": "meta", "redactEnabled": False, "outputMode": "r2"},
        ]
    )
    agent = _agent(tmp_path, opener)
    agent.cycle = lambda now=None: False  # type: ignore[method-assign]

    class _Stop(Exception):
        pass

    def stop(_seconds: float) -> None:
        raise _Stop

    original_sleep = module.time.sleep
    module.time.sleep = stop
    try:
        with pytest.raises(_Stop):
            agent.run()
    finally:
        module.time.sleep = original_sleep

    assert _paths(opener)[0] == "/agent/v1/config"
    assert agent.report_level == "meta"
    assert agent.redact_enabled is False
    assert agent.output_mode == "r2"
