from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
from pathlib import Path

from .config import Settings
from .models import (
    ProcessInfoResult,
    ProcessListResult,
    ProcessSignalResult,
    ServiceActionResult,
    ServiceStatusResult,
    SystemInfoResult,
)

# Exact unit names only. Refuse option-like values up front even though subprocess is
# never invoked through a shell, then also use `--` before the unit argument.
_SERVICE_RE = re.compile(r"^[A-Za-z0-9_.@][A-Za-z0-9_.@:-]{0,199}$")
_ALLOWED_SIGNALS = {
    "SIGINT": signal.SIGINT,
    "SIGHUP": signal.SIGHUP,
    "SIGTERM": signal.SIGTERM,
    "SIGKILL": signal.SIGKILL,
    "SIGSTOP": signal.SIGSTOP,
    "SIGCONT": signal.SIGCONT,
}


def _proc_stat(pid: int) -> tuple[str, int, int, int, int]:
    if pid <= 0:
        raise ValueError("pid must be > 0")
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("Process does not exist or cannot be inspected") from exc
    close = raw.rfind(")")
    if close < 0:
        raise ValueError("Process stat is malformed")
    fields = raw[close + 2 :].split()
    if len(fields) <= 19:
        raise ValueError("Process stat is incomplete")
    try:
        state = fields[0]
        ppid = int(fields[1])
        pgid = int(fields[2])
        session_id = int(fields[3])
        start_ticks = int(fields[19])
    except ValueError as exc:
        raise ValueError("Process stat is malformed") from exc
    return state, ppid, pgid, session_id, start_ticks


def pidfd_supported() -> bool:
    """Return whether this Python/Linux runtime exposes both pidfd operations."""

    return callable(getattr(os, "pidfd_open", None)) and callable(getattr(signal, "pidfd_send_signal", None))


def _open_verified_pidfd(pid: int, expected_start_ticks: int) -> int | None:
    """Open a stable process handle without weakening restart-safe PID identity.

    Persistent state still stores PID + /proc starttime because pidfds do not
    survive a Remote Host MCP restart. While the server is online, a pidfd closes the small
    race between the identity check and signal delivery. The identity is checked
    once before and once after pidfd_open so a reused PID cannot be adopted.
    """

    _state, _ppid, _pgid, _session_id, before = _proc_stat(pid)
    if before != expected_start_ticks:
        raise ValueError("Process identity mismatch; refusing to signal a reused/stale PID")
    if not pidfd_supported():
        return None
    try:
        fd = os.pidfd_open(pid, 0)  # type: ignore[attr-defined]
    except ProcessLookupError as exc:
        raise ValueError("Process disappeared before pidfd acquisition") from exc
    except OSError:
        # Some container/seccomp policies expose the Python API while denying the
        # syscall. Falling back is safe because we revalidate immediately below.
        return None
    try:
        _state, _ppid, _pgid, _session_id, after = _proc_stat(pid)
    except ValueError:
        os.close(fd)
        raise
    if after != expected_start_ticks:
        os.close(fd)
        raise ValueError("Process identity changed during pidfd acquisition")
    return fd


def _uid_gid(pid: int) -> tuple[int | None, int | None]:
    try:
        lines = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, None
    uid: int | None = None
    gid: int | None = None
    for line in lines:
        if line.startswith("Uid:"):
            try:
                uid = int(line.split()[1])
            except (IndexError, ValueError):
                pass
        elif line.startswith("Gid:"):
            try:
                gid = int(line.split()[1])
            except (IndexError, ValueError):
                pass
    return uid, gid


def _readlink(path: Path) -> str | None:
    try:
        return os.readlink(path)
    except OSError:
        return None


def _cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    parts = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]
    text = " ".join(parts)
    encoded = text.encode("utf-8")
    if len(encoded) <= 4096:
        return text
    return encoded[:4096].decode("utf-8", errors="ignore") + "…"


def process_info(pid: int) -> ProcessInfoResult:
    state, ppid, pgid, session_id, start_ticks = _proc_stat(pid)
    uid, gid = _uid_gid(pid)
    return ProcessInfoResult(
        pid=pid,
        start_ticks=start_ticks,
        ppid=ppid,
        pgid=pgid,
        session_id=session_id,
        state=state,
        uid=uid,
        gid=gid,
        cmdline=_cmdline(pid),
        cwd=_readlink(Path(f"/proc/{pid}/cwd")),
        executable=_readlink(Path(f"/proc/{pid}/exe")),
        alive=True,
    )


def process_list(limit: int) -> ProcessListResult:
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    numeric: list[int] = []
    try:
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                numeric.append(int(entry.name))
    except OSError as exc:
        raise ValueError("/proc is unavailable") from exc
    numeric.sort()
    rows: list[ProcessInfoResult] = []
    truncated = False
    for pid in numeric:
        try:
            rows.append(process_info(pid))
        except ValueError:
            continue
        if len(rows) > limit:
            rows.pop()
            truncated = True
            break
    return ProcessListResult(processes=rows, truncated=truncated)


def process_signal(pid: int, expected_start_ticks: int, signal_name: str) -> ProcessSignalResult:
    name = signal_name.strip().upper()
    if not name.startswith("SIG"):
        name = "SIG" + name
    sig = _ALLOWED_SIGNALS.get(name)
    if sig is None:
        raise ValueError(f"signal must be one of {sorted(_ALLOWED_SIGNALS)}")

    pidfd = _open_verified_pidfd(pid, expected_start_ticks)
    if pidfd is not None:
        try:
            signal.pidfd_send_signal(pidfd, sig)  # type: ignore[attr-defined]
        except ProcessLookupError as exc:
            raise ValueError("Process disappeared before signal delivery") from exc
        finally:
            os.close(pidfd)
    else:
        # Fallback for old/restricted kernels: verify immediately before kill().
        _state, _ppid, _pgid, _session_id, current_start = _proc_stat(pid)
        if current_start != expected_start_ticks:
            raise ValueError("Process identity mismatch; refusing to signal a reused/stale PID")
        try:
            os.kill(pid, sig)
        except ProcessLookupError as exc:
            raise ValueError("Process disappeared before signal delivery") from exc

    alive_after = Path(f"/proc/{pid}").exists()
    return ProcessSignalResult(
        success=True,
        pid=pid,
        start_ticks=expected_start_ticks,
        signal=name,
        alive_after=alive_after,
    )


def _service_name(service: str) -> str:
    value = service.strip()
    if not _SERVICE_RE.fullmatch(value):
        raise ValueError("service contains unsupported characters")
    return value


def _systemctl_env() -> dict[str, str]:
    env = dict(os.environ)
    env["TERM"] = "dumb"
    env["PAGER"] = "cat"
    env["SYSTEMD_PAGER"] = "cat"
    env["NO_COLOR"] = "1"
    return env


def _bounded(text: str, limit: int = 8192) -> str:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore") + "\n[truncated]"


def service_status(service: str) -> ServiceStatusResult:
    name = _service_name(service)
    if shutil.which("systemctl") is None:
        return ServiceStatusResult(service=name, available=False, return_code=None, output="systemctl is unavailable")
    try:
        proc = subprocess.run(
            [
                "systemctl",
                "show",
                "--no-pager",
                "--property=ActiveState",
                "--property=SubState",
                "--property=UnitFileState",
                "--",
                name,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            env=_systemctl_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ServiceStatusResult(
            service=name,
            available=False,
            return_code=None,
            output=f"systemctl status failed: {type(exc).__name__}",
        )
    values: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    output = _bounded((proc.stderr or proc.stdout).strip())
    return ServiceStatusResult(
        service=name,
        available=True,
        active_state=values.get("ActiveState"),
        sub_state=values.get("SubState"),
        unit_file_state=values.get("UnitFileState"),
        return_code=proc.returncode,
        output=output,
    )


def service_action(service: str, action: str) -> ServiceActionResult:
    name = _service_name(service)
    verb = action.strip().lower()
    if verb not in {"start", "stop", "restart"}:
        raise ValueError("action must be start, stop, or restart")
    if shutil.which("systemctl") is None:
        return ServiceActionResult(
            success=False,
            service=name,
            action=verb,
            return_code=None,
            output="systemctl is unavailable",
            status=service_status(name),
        )
    try:
        proc = subprocess.run(
            ["systemctl", "--no-pager", verb, "--", name],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            env=_systemctl_env(),
        )
        output = _bounded((proc.stderr or proc.stdout).strip())
        return ServiceActionResult(
            success=proc.returncode == 0,
            service=name,
            action=verb,
            return_code=proc.returncode,
            output=output,
            status=service_status(name),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ServiceActionResult(
            success=False,
            service=name,
            action=verb,
            return_code=None,
            output=f"systemctl action failed: {type(exc).__name__}",
            status=service_status(name),
        )


def _meminfo() -> tuple[int | None, int | None]:
    try:
        lines = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, None
    values: dict[str, int] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        parts = rest.strip().split()
        if not parts:
            continue
        try:
            values[key] = int(parts[0]) * 1024
        except ValueError:
            continue
    return values.get("MemTotal"), values.get("MemAvailable")


def _uptime() -> float | None:
    try:
        return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def system_info(settings: Settings) -> SystemInfoResult:
    mem_total, mem_available = _meminfo()
    try:
        load = list(os.getloadavg())
    except OSError:
        load = []
    disk_path = settings.allowed_roots[0]
    if not disk_path.exists():
        disk_path = settings.state_dir if settings.state_dir.exists() else Path("/")
    usage = shutil.disk_usage(disk_path)
    uname = os.uname()
    return SystemInfoResult(
        hostname=socket.gethostname(),
        kernel=f"{uname.sysname} {uname.release}",
        architecture=uname.machine,
        cpu_count=os.cpu_count(),
        load_average=load,
        uptime_seconds=_uptime(),
        memory_total_bytes=mem_total,
        memory_available_bytes=mem_available,
        disk_path=str(disk_path),
        disk_total_bytes=usage.total,
        disk_used_bytes=usage.used,
        disk_free_bytes=usage.free,
        state_dir=str(settings.state_dir),
        allowed_roots=[str(path) for path in settings.allowed_roots],
    )
