from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import __version__
from .atomic_fs import renameat2_available
from .compat_env import env_namespace_summary
from .config import ConfigError, Settings
from .provenance import get_build_provenance
from .secure_paths import openat2_supported
from .system_helpers import _systemd_manager_capability


def collect_report(settings: Settings, *, sources: dict[str, str] | None = None) -> dict[str, Any]:
    build_commit, build_ref = get_build_provenance()
    try:
        openat2 = openat2_supported(settings)
    except (OSError, ValueError):
        openat2 = False
    systemctl_present, pid1_is_systemd, manager_reachable, reason, _probe_code, _probe_output = (
        _systemd_manager_capability()
    )
    state_parent = settings.state_dir if settings.state_dir.exists() else settings.state_dir.parent
    return {
        "service": "Remote Host MCP",
        "version": __version__,
        "build": {"commit": build_commit, "ref": build_ref},
        "configuration": {
            "auth_mode": settings.auth_mode,
            "public_host": settings.public_host,
            "public_url": settings.redacted_public_url,
            "bind_host": settings.bind_host,
            "port": settings.port,
            "state_dir": str(settings.state_dir),
            "allowed_roots": [str(root) for root in settings.allowed_roots],
            "namespace_sources": sources if sources is not None else env_namespace_summary(),
        },
        "capabilities": {
            "openat2": openat2,
            "renameat2": renameat2_available(),
            "state_dir_parent_writable": os.access(state_parent, os.W_OK),
            "allowed_roots_exist": all(Path(root).exists() for root in settings.allowed_roots),
            "systemd": {
                "systemctl_present": systemctl_present,
                "pid1_is_systemd": pid1_is_systemd,
                "manager_reachable": manager_reachable,
                "operational": manager_reachable,
                "reason": reason,
            },
        },
    }


def main() -> None:
    sources = env_namespace_summary()
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        raise SystemExit(2) from exc
    report = collect_report(settings, sources=sources)
    report["ok"] = True
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
