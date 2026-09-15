from __future__ import annotations

from pathlib import Path

import pytest

from remote_host_mcp.hub_settings import HubConfigError, HubSettings


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(__import__("os").environ):
        if name.startswith("RHMCP_HUB_"):
            monkeypatch.delenv(name, raising=False)


def test_defaults_are_safe_without_any_hub_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    hub = HubSettings.from_env()
    assert hub.configured is False
    assert hub.reporting_active is False
    assert hub.redact_enabled is True
    assert hub.audit_enabled is True
    assert hub.report_level == "tool"
    assert hub.output_mode == "preview"


def test_reads_the_full_documented_variable_block(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("RHMCP_HUB_BASE_URL", "https://hub.example.com/")
    monkeypatch.setenv("RHMCP_HUB_HOST_ID", "host_abc")
    monkeypatch.setenv("RHMCP_HUB_AGENT_KEY", "agt_deadbeef")
    monkeypatch.setenv("RHMCP_HUB_REPORT_LEVEL", "FULL")
    monkeypatch.setenv("RHMCP_HUB_REDACT_ENABLED", "false")
    monkeypatch.setenv("RHMCP_HUB_OUTPUT_MODE", "r2")
    hub = HubSettings.from_env(strict=True)
    assert hub.base_url == "https://hub.example.com"
    assert hub.host_id == "host_abc"
    assert hub.agent_key == "agt_deadbeef"
    assert hub.report_level == "full"
    assert hub.redact_enabled is False
    assert hub.output_mode == "r2"
    assert hub.configured is True
    assert hub.reporting_active is True


def test_report_switch_disables_reporting_without_disabling_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("RHMCP_HUB_BASE_URL", "https://hub.example.com")
    monkeypatch.setenv("RHMCP_HUB_HOST_ID", "host_abc")
    monkeypatch.setenv("RHMCP_HUB_AGENT_KEY", "agt_deadbeef")
    monkeypatch.setenv("RHMCP_HUB_REPORT_ENABLED", "false")
    hub = HubSettings.from_env(strict=True)
    assert hub.reporting_active is False
    assert hub.audit_enabled is True


def test_lenient_mode_ignores_bad_values_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("RHMCP_HUB_BASE_URL", "http://insecure.example.com")
    monkeypatch.setenv("RHMCP_HUB_REPORT_LEVEL", "everything")
    monkeypatch.setenv("RHMCP_HUB_REDACT_ENABLED", "maybe")
    monkeypatch.setenv("RHMCP_HUB_AGENT_KEY", "not-a-key")
    hub = HubSettings.from_env()
    assert hub.base_url is None
    assert hub.report_level == "tool"
    assert hub.redact_enabled is True
    assert hub.agent_key is None


def test_strict_mode_rejects_bad_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("RHMCP_HUB_BASE_URL", "http://insecure.example.com")
    with pytest.raises(HubConfigError):
        HubSettings.from_env(strict=True)


def test_audit_log_path_defaults_under_the_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("RHMCP_STATE_DIR", str(tmp_path))
    hub = HubSettings.from_env()
    assert hub.resolved_audit_log_path() == tmp_path / "audit" / "calls.jsonl"


def test_audit_log_path_override_is_honoured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear(monkeypatch)
    target = tmp_path / "elsewhere" / "audit.jsonl"
    monkeypatch.setenv("RHMCP_HUB_AUDIT_LOG_PATH", str(target))
    assert HubSettings.from_env(strict=True).resolved_audit_log_path() == target


def test_relative_audit_log_path_is_rejected_in_strict_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("RHMCP_HUB_AUDIT_LOG_PATH", "relative/audit.jsonl")
    with pytest.raises(HubConfigError):
        HubSettings.from_env(strict=True)
