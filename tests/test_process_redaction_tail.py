from remote_host_mcp.system_helpers import _redact_argv


def test_secret_flag_value_at_end_of_argv_is_redacted() -> None:
    fake_secret = "FAKE_SECRET_END_OF_ARGV"
    rendered = " ".join(
        _redact_argv(["demo", "safe-before", "--secret", fake_secret])
    )
    assert fake_secret not in rendered
    assert rendered.endswith("--secret <redacted>")
    assert "safe-before" in rendered
