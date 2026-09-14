from __future__ import annotations

import subprocess

import pytest

from remote_host_mcp import system_helpers
from remote_host_mcp.models import ServiceStatusResult
from remote_host_mcp.system_helpers import service_action, service_status


class _Proc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_service_status_reports_systemctl_missing_without_invoking_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_helpers.shutil, "which", lambda _name: None)
    monkeypatch.setattr(system_helpers, "_pid1_is_systemd", lambda: False)

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("subprocess.run must not execute when systemctl is missing")

    monkeypatch.setattr(system_helpers.subprocess, "run", unexpected_run)

    status = service_status("rhmcp-smoke.service")
    assert status.available is False
    assert status.systemctl_present is False
    assert status.pid1_is_systemd is False
    assert status.manager_reachable is False
    assert status.operational is False
    assert status.reason == "systemctl_missing"

    action = service_action("rhmcp-smoke.service", "restart")
    assert action.success is False
    assert action.return_code is None
    assert action.status.reason == "systemctl_missing"
    assert "systemctl_missing" in action.output


def test_non_systemd_host_is_structured_and_service_action_has_no_action_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_helpers.shutil, "which", lambda _name: "/usr/bin/systemctl")
    monkeypatch.setattr(system_helpers, "_pid1_is_systemd", lambda: False)
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        assert argv[:2] == ["systemctl", "show"]
        return _Proc(
            1,
            stderr=(
                "System has not been booted with systemd as init system (PID 1). "
                "Failed to connect to bus"
            ),
        )

    monkeypatch.setattr(system_helpers.subprocess, "run", fake_run)

    status = service_status("rhmcp-smoke.service")
    assert status.available is True  # backward-compatible: binary is present
    assert status.systemctl_present is True
    assert status.pid1_is_systemd is False
    assert status.manager_reachable is False
    assert status.operational is False
    assert status.reason == "manager_unreachable_pid1_not_systemd"
    assert "Failed to connect to bus" in status.output

    action = service_action("rhmcp-smoke.service", "restart")
    assert action.success is False
    assert action.return_code is None
    assert action.status.operational is False
    assert action.status.reason == "manager_unreachable_pid1_not_systemd"
    assert "manager_unreachable_pid1_not_systemd" in action.output

    assert calls
    assert all(call[:2] == ["systemctl", "show"] for call in calls)
    assert all(not (len(call) > 2 and call[2] in {"start", "stop", "restart"}) for call in calls)


def test_operational_systemd_manager_allows_exact_service_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_helpers.shutil, "which", lambda _name: "/usr/bin/systemctl")
    monkeypatch.setattr(system_helpers, "_pid1_is_systemd", lambda: True)
    state = {"active": "active", "sub": "running"}
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        argv = list(argv)
        calls.append(argv)
        if argv == ["systemctl", "show", "--no-pager", "--property=Version", "--value"]:
            return _Proc(0, stdout="255\n")
        if "--property=ActiveState" in argv:
            return _Proc(
                0,
                stdout=(
                    f"ActiveState={state['active']}\n"
                    f"SubState={state['sub']}\n"
                    "UnitFileState=transient\n"
                ),
            )
        if argv == ["systemctl", "--no-pager", "restart", "--", "rhmcp-smoke.service"]:
            state.update(active="active", sub="running")
            return _Proc(0)
        if argv == ["systemctl", "--no-pager", "stop", "--", "rhmcp-smoke.service"]:
            state.update(active="inactive", sub="dead")
            return _Proc(0)
        raise AssertionError(f"unexpected systemctl argv: {argv}")

    monkeypatch.setattr(system_helpers.subprocess, "run", fake_run)

    before = service_status("rhmcp-smoke.service")
    assert before.available is True
    assert before.systemctl_present is True
    assert before.pid1_is_systemd is True
    assert before.manager_reachable is True
    assert before.operational is True
    assert before.reason is None
    assert before.active_state == "active"

    restarted = service_action("rhmcp-smoke.service", "restart")
    assert restarted.success is True
    assert restarted.status.operational is True
    assert restarted.status.active_state == "active"

    stopped = service_action("rhmcp-smoke.service", "stop")
    assert stopped.success is True
    assert stopped.status.operational is True
    assert stopped.status.active_state == "inactive"
    assert ["systemctl", "--no-pager", "restart", "--", "rhmcp-smoke.service"] in calls
    assert ["systemctl", "--no-pager", "stop", "--", "rhmcp-smoke.service"] in calls


def test_service_status_schema_is_additive_and_machine_readable() -> None:
    schema = ServiceStatusResult.model_json_schema()
    properties = schema["properties"]
    for field in (
        "available",
        "systemctl_present",
        "pid1_is_systemd",
        "manager_reachable",
        "operational",
        "reason",
    ):
        assert field in properties
