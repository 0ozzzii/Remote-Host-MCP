from __future__ import annotations

import importlib.metadata

from remote_host_mcp import __version__


def test_runtime_version_matches_installed_distribution() -> None:
    assert __version__ == "0.1.0a1"
    assert __version__ == importlib.metadata.version("remote-host-mcp")


def test_legacy_namespace_keeps_same_runtime_version() -> None:
    from dsw_direct_mcp import __version__ as legacy_version

    assert legacy_version == __version__
