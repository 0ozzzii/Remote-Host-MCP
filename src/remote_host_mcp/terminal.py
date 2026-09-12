from __future__ import annotations

import asyncio
import os
import re
import secrets
import shlex
import shutil
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pexpect
import pyte

from .config import Settings
from .models import (
    TerminalActionResult,
    TerminalExecResult,
    TerminalInfo,
    TerminalListResult,
    TerminalOpenResult,
    TerminalReadResult,
    TerminalWriteResult,
)
from .terminal_models import TerminalScreenResult

_SECRET_ENV_NAMES = {
    "RHMCP_PATH_KEY",
    "DSW_MCP_PATH_KEY",
    "CLOUDFLARED_TOKEN",
    "CF_TUNNEL_TOKEN",
    "TUNNEL_TOKEN",
    "CFD_TOKEN",
}
_TERMINAL_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_COMPLETING_SIGNALS = {"SIGINT", "SIGQUIT", "SIGTERM", "SIGHUP"}
_RHMCP_OSC_PREFIX = b"\x1b]133;"
_RHMCP_OSC_RE = re.compile(rb"\x1b\]133;([CD]);id=([a-f0-9]{24})(?:;rc=(-?\d+))?\x07")


def _safe_terminal_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key.upper() in _SECRET_ENV_NAMES:
            env.pop(key, None)
    env["TERM"] = "xterm-256color"
    env.setdefault("COLORTERM", "truecolor")
    return env


def _redact(text: str, settings: Settings) -> str:
    return text.replace(settings.path_key, "[REDACTED]") if settings.path_key else text


@dataclass(slots=True)
class PendingCommand:
    command_id: str
    start_cursor: int
    done_path: Path
    event: asyncio.Event
    exit_code: int | None = None
    end_cursor: int | None = None
    completed: bool = False
    signal_name: str | None = None
    signal_number: int | None = None
    foreground_pgid: int | None = None
    saw_non_shell_foreground: bool = False
    semantic_exit_code: int | None = None
    completion_source: str | None = None


@dataclass(slots=True)
class TerminalSession:
    terminal_id: str
    child: pexpect.spawn
    shell: str
    initial_cwd: str
    cols: int
    rows: int
    created_at: int
    last_activity_at: int
    max_buffer_bytes: int
    state_dir: Path
    settings: Settings
    buffer: bytearray = field(default_factory=bytearray)
    buffer_start: int = 0
    stop_reader: threading.Event = field(default_factory=threading.Event)
    condition: threading.Condition = field(default_factory=threading.Condition)
    write_lock: threading.Lock = field(default_factory=threading.Lock)
    reader_thread: threading.Thread | None = None
    pending: PendingCommand | None = None
    pending_task: asyncio.Task[None] | None = None
    closed: bool = False
    screen: pyte.Screen | None = None
    stream: pyte.Stream | None = None
    osc_tail: bytes = b""

    @property
    def buffer_end(self) -> int:
        return self.buffer_start + len(self.buffer)

    def alive(self) -> bool:
        if self.closed:
            return False
        try:
            return bool(self.child.isalive())
        except Exception:
            return False

    def exit_code(self) -> int | None:
        if self.alive():
            return None
        if self.child.exitstatus is not None:
            return int(self.child.exitstatus)
        if self.child.signalstatus is not None:
            return -int(self.child.signalstatus)
        return None

    def current_cwd(self) -> str:
        try:
            return os.readlink(f"/proc/{self.child.pid}/cwd")
        except OSError:
            return self.initial_cwd

    def _strip_rhmcp_osc133(self, data: bytes) -> bytes:
        """Consume Remote Host MCP-owned OSC 133 markers and retain split markers across reads."""

        combined = self.osc_tail + data
        self.osc_tail = b""

        def consume(match: re.Match[bytes]) -> bytes:
            kind = match.group(1)
            command_id = match.group(2).decode("ascii")
            raw_rc = match.group(3)
            pending = self.pending
            if (
                kind == b"D"
                and raw_rc is not None
                and pending is not None
                and not pending.completed
                and pending.command_id == command_id
            ):
                try:
                    pending.semantic_exit_code = int(raw_rc)
                except ValueError:
                    pass
            return b""

        cleaned = _RHMCP_OSC_RE.sub(consume, combined)

        # Keep an incomplete Remote Host MCP marker out of raw output until the next PTY read.
        marker_at = cleaned.rfind(_RHMCP_OSC_PREFIX)
        if marker_at >= 0 and b"\x07" not in cleaned[marker_at:]:
            self.osc_tail = cleaned[marker_at:]
            cleaned = cleaned[:marker_at]
        else:
            # Also preserve a split prefix such as ESC, ESC], or ESC]13.
            max_prefix = min(len(_RHMCP_OSC_PREFIX) - 1, len(cleaned))
            for size in range(max_prefix, 0, -1):
                if cleaned[-size:] == _RHMCP_OSC_PREFIX[:size]:
                    self.osc_tail = cleaned[-size:]
                    cleaned = cleaned[:-size]
                    break
        return cleaned

    def append_output(self, data: bytes) -> None:
        if not data:
            return
        data = self._strip_rhmcp_osc133(data)
        if not data:
            return
        with self.condition:
            self.buffer.extend(data)
            excess = len(self.buffer) - self.max_buffer_bytes
            if excess > 0:
                del self.buffer[:excess]
                self.buffer_start += excess
            if self.stream is not None:
                try:
                    self.stream.feed(data.decode("utf-8", errors="replace"))
                except Exception:
                    pass
            self.last_activity_at = int(time.time())
            self.condition.notify_all()

    def reader_loop(self) -> None:
        while not self.stop_reader.is_set():
            try:
                data = self.child.read_nonblocking(size=65536, timeout=0.2)
            except pexpect.TIMEOUT:
                continue
            except pexpect.EOF:
                break
            except Exception:
                break
            if isinstance(data, str):
                data = data.encode("utf-8", errors="replace")
            self.append_output(data)
        # If the shell dies halfway through an escape sequence, preserve those bytes
        # as ordinary output rather than silently dropping user-visible data.
        if self.osc_tail:
            tail = self.osc_tail
            self.osc_tail = b""
            with self.condition:
                self.buffer.extend(tail)
                self.condition.notify_all()
        with self.condition:
            self.condition.notify_all()

    def start_reader(self) -> None:
        self.reader_thread = threading.Thread(
            target=self.reader_loop,
            name=f"rhmcp-pty-{self.terminal_id[:8]}",
            daemon=True,
        )
        self.reader_thread.start()

    def send_bytes(self, data: bytes) -> int:
        if not self.alive():
            raise ValueError("Terminal is not alive")
        with self.write_lock:
            sent = self.child.send(data)
        self.last_activity_at = int(time.time())
        return int(sent)

    def shell_pgid(self) -> int | None:
        try:
            return os.getpgid(self.child.pid)
        except (OSError, ProcessLookupError):
            return None

    def foreground_pgid(self) -> int | None:
        try:
            pgid = os.tcgetpgrp(self.child.child_fd)
        except OSError:
            return None
        return pgid if pgid > 0 else None

    def read_slice(self, cursor: int | None, max_bytes: int, wait_ms: int) -> tuple[int, int, bytes, bool, bool]:
        deadline = time.monotonic() + wait_ms / 1000
        with self.condition:
            requested = self.buffer_start if cursor is None else cursor
            if requested < 0:
                raise ValueError("cursor must be >= 0")
            while requested >= self.buffer_end and self.alive() and wait_ms > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(timeout=min(remaining, 0.25))

            truncated = requested < self.buffer_start
            effective = max(requested, self.buffer_start)
            if effective > self.buffer_end:
                raise ValueError("cursor is beyond current terminal output")
            relative = effective - self.buffer_start
            data = bytes(self.buffer[relative : relative + max_bytes])
            next_cursor = effective + len(data)
            at_end = next_cursor >= self.buffer_end
            return effective, next_cursor, data, truncated, at_end

    def output_between(self, start: int, end: int | None, max_bytes: int) -> tuple[int, int, bytes, bool]:
        with self.condition:
            truncated = start < self.buffer_start
            effective = max(start, self.buffer_start)
            upper = self.buffer_end if end is None else min(end, self.buffer_end)
            if effective > upper:
                effective = upper
            relative = effective - self.buffer_start
            available = max(0, upper - effective)
            take = min(max_bytes, available)
            data = bytes(self.buffer[relative : relative + take])
            return effective, effective + len(data), data, truncated

    def info(self) -> TerminalInfo:
        with self.condition:
            start = self.buffer_start
            end = self.buffer_end
        return TerminalInfo(
            terminal_id=self.terminal_id,
            pid=self.child.pid,
            shell=self.shell,
            cwd=self.current_cwd(),
            cols=self.cols,
            rows=self.rows,
            created_at=self.created_at,
            last_activity_at=self.last_activity_at,
            alive=self.alive(),
            busy=self.pending is not None and not self.pending.completed,
            exit_code=self.exit_code(),
            buffer_start=start,
            buffer_end=end,
        )


class TerminalManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._sessions: dict[str, TerminalSession] = {}
        self._lock = asyncio.Lock()
        self._janitor_task: asyncio.Task[None] | None = None
        self._state_root = settings.state_dir / "terminals"
        self._state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self._state_root, 0o700)
        except OSError:
            pass

    def _validate_id(self, terminal_id: str) -> str:
        if not _TERMINAL_ID_RE.fullmatch(terminal_id):
            raise ValueError("Invalid terminal_id")
        return terminal_id

    def _session(self, terminal_id: str) -> TerminalSession:
        terminal_id = self._validate_id(terminal_id)
        session = self._sessions.get(terminal_id)
        if session is None:
            raise ValueError("Unknown terminal_id")
        return session

    def _ensure_janitor(self) -> None:
        if self._janitor_task is None or self._janitor_task.done():
            self._janitor_task = asyncio.create_task(self._janitor_loop(), name="rhmcp-terminal-janitor")

    async def _janitor_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(15)
                now = int(time.time())
                stale: list[str] = []
                async with self._lock:
                    for terminal_id, session in self._sessions.items():
                        if not session.alive():
                            stale.append(terminal_id)
                            continue
                        if now - session.last_activity_at >= self.settings.terminal_idle_seconds:
                            stale.append(terminal_id)
                            continue
                        if now - session.created_at >= self.settings.terminal_max_lifetime_seconds:
                            stale.append(terminal_id)
                for terminal_id in stale:
                    try:
                        await self.close(terminal_id)
                    except ValueError:
                        pass
                async with self._lock:
                    if not self._sessions:
                        return
        except asyncio.CancelledError:
            return

    async def open(self, cwd: str | None, cols: int, rows: int) -> TerminalOpenResult:
        if cols < 20 or cols > 500:
            raise ValueError("cols must be between 20 and 500")
        if rows < 5 or rows > 300:
            raise ValueError("rows must be between 5 and 300")
        if cwd is None:
            try:
                safe_cwd = self.settings.resolve_allowed_path(None, default=Path.cwd())
            except ValueError:
                safe_cwd = self.settings.allowed_roots[0]
        else:
            safe_cwd = self.settings.resolve_allowed_path(cwd)
        if not safe_cwd.exists() or not safe_cwd.is_dir():
            raise ValueError("cwd is not an existing allowed directory")

        async with self._lock:
            live = [session for session in self._sessions.values() if session.alive()]
            if len(live) >= self.settings.terminal_max_sessions:
                raise ValueError(f"Terminal session limit reached ({self.settings.terminal_max_sessions})")

            terminal_id = secrets.token_hex(16)
            session_state = self._state_root / terminal_id
            session_state.mkdir(mode=0o700)
            shell = "/bin/bash" if Path("/bin/bash").exists() else "/bin/sh"
            args = ["-l"] if shell.endswith("bash") else []
            child = pexpect.spawn(
                shell,
                args=args,
                cwd=str(safe_cwd),
                env=_safe_terminal_env(),
                encoding=None,
                echo=False,
                timeout=None,
                maxread=65536,
                dimensions=(rows, cols),
            )
            now = int(time.time())
            screen = pyte.Screen(cols, rows)
            stream = pyte.Stream(screen)
            session = TerminalSession(
                terminal_id=terminal_id,
                child=child,
                shell=shell,
                initial_cwd=str(safe_cwd),
                cols=cols,
                rows=rows,
                created_at=now,
                last_activity_at=now,
                max_buffer_bytes=self.settings.terminal_buffer_bytes,
                state_dir=session_state,
                settings=self.settings,
                screen=screen,
                stream=stream,
            )
            self._sessions[terminal_id] = session
            session.start_reader()
            self._ensure_janitor()

        await asyncio.sleep(0.05)
        with session.condition:
            cursor = session.buffer_start
        return TerminalOpenResult(
            terminal_id=terminal_id,
            pid=child.pid,
            shell=shell,
            cwd=str(safe_cwd),
            cols=cols,
            rows=rows,
            created_at=now,
            cursor=cursor,
        )

    async def read(self, terminal_id: str, cursor: int | None, max_bytes: int, wait_ms: int) -> TerminalReadResult:
        session = self._session(terminal_id)
        if max_bytes < 1 or max_bytes > self.settings.terminal_read_max_bytes:
            raise ValueError(f"max_bytes must be between 1 and {self.settings.terminal_read_max_bytes}")
        if wait_ms < 0 or wait_ms > self.settings.terminal_wait_max_ms:
            raise ValueError(f"wait_ms must be between 0 and {self.settings.terminal_wait_max_ms}")
        effective, next_cursor, data, truncated, at_end = await asyncio.to_thread(
            session.read_slice, cursor, max_bytes, wait_ms
        )
        session.last_activity_at = int(time.time())
        return TerminalReadResult(
            terminal_id=terminal_id,
            cursor=effective,
            next_cursor=next_cursor,
            text=_redact(data.decode("utf-8", errors="replace"), self.settings),
            bytes_returned=len(data),
            truncated_before_cursor=truncated,
            at_buffer_end=at_end,
            alive=session.alive(),
            exit_code=session.exit_code(),
        )

    async def write(self, terminal_id: str, data: str, append_enter: bool) -> TerminalWriteResult:
        session = self._session(terminal_id)
        raw = data.encode("utf-8") + (b"\n" if append_enter else b"")
        if len(raw) > 65_536:
            raise ValueError("terminal_write payload exceeds 65536 UTF-8 bytes")
        written = await asyncio.to_thread(session.send_bytes, raw)
        return TerminalWriteResult(terminal_id=terminal_id, bytes_written=written, alive=session.alive())

    async def resize(self, terminal_id: str, cols: int, rows: int) -> TerminalActionResult:
        session = self._session(terminal_id)
        if cols < 20 or cols > 500 or rows < 5 or rows > 300:
            raise ValueError("terminal size is outside allowed bounds")
        if not session.alive():
            raise ValueError("Terminal is not alive")
        await asyncio.to_thread(session.child.setwinsize, rows, cols)
        with session.condition:
            session.cols = cols
            session.rows = rows
            if session.screen is not None:
                session.screen.resize(lines=rows, columns=cols)
        session.last_activity_at = int(time.time())
        return TerminalActionResult(success=True, terminal_id=terminal_id, action=f"resize:{cols}x{rows}", alive=True)

    async def signal(self, terminal_id: str, signal_name: str) -> TerminalActionResult:
        session = self._session(terminal_id)
        name = signal_name.strip().upper()
        if not name.startswith("SIG"):
            name = "SIG" + name
        allowed = {"SIGINT", "SIGQUIT", "SIGTSTP", "SIGTERM", "SIGHUP", "SIGCONT"}
        if name not in allowed:
            raise ValueError(f"signal must be one of {sorted(allowed)}")
        if not session.alive():
            return TerminalActionResult(success=True, terminal_id=terminal_id, action=name, alive=False)

        sig = getattr(signal, name)
        pending = session.pending if session.pending is not None and not session.pending.completed else None
        foreground = session.foreground_pgid()
        shell_pgid = session.shell_pgid()
        if pending is not None:
            if foreground is not None and shell_pgid is not None and foreground != shell_pgid:
                pending.saw_non_shell_foreground = True
                pending.foreground_pgid = foreground
            if name in _COMPLETING_SIGNALS:
                pending.signal_name = name
                pending.signal_number = int(sig)

        try:
            if foreground is None:
                raise OSError("PTY foreground process group unavailable")
            os.killpg(foreground, sig)
        except (OSError, ProcessLookupError):
            # Preserve terminal semantics for classic control keys if the platform
            # cannot expose the foreground pgrp. Do not use fuzzy process matching.
            control = {"SIGINT": b"\x03", "SIGQUIT": b"\x1c", "SIGTSTP": b"\x1a"}
            if name in control:
                try:
                    await asyncio.to_thread(session.send_bytes, control[name])
                except ValueError:
                    pass
            else:
                try:
                    os.kill(session.child.pid, sig)
                except ProcessLookupError:
                    pass
        session.last_activity_at = int(time.time())
        return TerminalActionResult(success=True, terminal_id=terminal_id, action=name, alive=session.alive())

    @staticmethod
    def _mark_pending_complete(
        session: TerminalSession,
        pending: PendingCommand,
        exit_code: int | None,
        source: str,
    ) -> None:
        pending.exit_code = exit_code
        pending.completion_source = source
        with session.condition:
            pending.end_cursor = session.buffer_end
        pending.completed = True
        pending.event.set()

    async def _monitor_command(self, session: TerminalSession, pending: PendingCommand) -> None:
        try:
            while True:
                if pending.completed:
                    return

                # Preferred path: the shell itself emits a Remote Host MCP-owned OSC 133 D
                # marker with the exact exit status. Reader-side parsing strips the
                # marker from user/model output and publishes only semantic state.
                if pending.semantic_exit_code is not None:
                    await asyncio.sleep(0.03)
                    self._mark_pending_complete(session, pending, pending.semantic_exit_code, "osc133")
                    pending.done_path.unlink(missing_ok=True)
                    return

                # Compatibility path: the shell reaches the durable post-command
                # marker and records the exact shell exit status.
                if pending.done_path.exists():
                    try:
                        raw = pending.done_path.read_text(encoding="utf-8").strip()
                        exit_code = int(raw)
                    except (OSError, ValueError):
                        exit_code = None
                    pending.done_path.unlink(missing_ok=True)
                    await asyncio.sleep(0.08)
                    self._mark_pending_complete(session, pending, exit_code, "sentinel")
                    return

                if not session.alive():
                    await asyncio.sleep(0.05)
                    self._mark_pending_complete(session, pending, session.exit_code(), "terminal_exit")
                    return

                # Interactive shells hand the PTY to an external foreground process
                # group and reclaim it when that job exits/stops. Record that handoff
                # as an OS-level fact. This is deliberately independent from output
                # text/prompts, aliases, locale, and shell theme.
                shell_pgid = session.shell_pgid()
                foreground = session.foreground_pgid()
                if shell_pgid is not None and foreground is not None:
                    if foreground != shell_pgid:
                        pending.saw_non_shell_foreground = True
                        pending.foreground_pgid = foreground
                    elif (
                        pending.saw_non_shell_foreground
                        and pending.signal_number is not None
                        and pending.signal_name in _COMPLETING_SIGNALS
                    ):
                        # Bash may abort the rest of an interactive command list after
                        # Ctrl+C/QUIT, so both semantic and done-file markers can be
                        # skipped. A verified pgrp hand-back remains the kernel truth.
                        await asyncio.sleep(0.12)
                        if pending.semantic_exit_code is not None or pending.done_path.exists():
                            continue
                        if session.alive() and session.foreground_pgid() == shell_pgid:
                            self._mark_pending_complete(
                                session,
                                pending,
                                -pending.signal_number,
                                "foreground_pgid",
                            )
                            return

                await asyncio.sleep(0.05)
        finally:
            if pending.completed and session.pending is pending:
                session.last_activity_at = int(time.time())

    async def exec(self, terminal_id: str, command: str, wait_ms: int) -> TerminalExecResult:
        session = self._session(terminal_id)
        raw_command = command.encode("utf-8")
        if not command.strip() or "\x00" in command or len(raw_command) > self.settings.max_command_bytes:
            raise ValueError("Command is empty, contains NUL, or exceeds RHMCP_MAX_COMMAND_BYTES")
        if wait_ms < 0 or wait_ms > self.settings.terminal_wait_max_ms:
            raise ValueError(f"wait_ms must be between 0 and {self.settings.terminal_wait_max_ms}")
        if not session.alive():
            raise ValueError("Terminal is not alive")
        if session.pending is not None and not session.pending.completed:
            raise ValueError("Terminal already has a foreground terminal_exec command; use read/write/signal until it completes")

        command_id = secrets.token_hex(12)
        done_path = session.state_dir / f"{command_id}.done"
        with session.condition:
            start_cursor = session.buffer_end
        pending = PendingCommand(command_id=command_id, start_cursor=start_cursor, done_path=done_path, event=asyncio.Event())
        session.pending = pending
        if self.settings.terminal_osc133_enabled:
            semantic_start = f"printf '\\033]133;C;id={command_id}\\007'; "
            semantic_done = f"printf '\\033]133;D;id={command_id};rc=%s\\007' \"$__rhmcp_rc\"; "
        else:
            semantic_start = ""
            semantic_done = ""
        wrapper = (
            semantic_start
            + f"eval -- {shlex.quote(command)}; "
            + "__rhmcp_rc=$?; "
            + semantic_done
            + f"printf '%s\\n' \"$__rhmcp_rc\" > {shlex.quote(str(done_path))}\n"
        )
        await asyncio.to_thread(session.send_bytes, wrapper.encode("utf-8"))
        session.pending_task = asyncio.create_task(self._monitor_command(session, pending), name=f"rhmcp-term-cmd-{command_id}")

        if wait_ms > 0:
            try:
                await asyncio.wait_for(asyncio.shield(pending.event.wait()), timeout=wait_ms / 1000)
            except asyncio.TimeoutError:
                pass

        completed = pending.completed
        end_cursor = pending.end_cursor if completed else None
        effective, next_cursor, data, truncated = session.output_between(
            start_cursor,
            end_cursor,
            self.settings.terminal_read_max_bytes,
        )
        if completed and session.pending is pending:
            session.pending = None
        session.last_activity_at = int(time.time())
        return TerminalExecResult(
            terminal_id=terminal_id,
            command_id=command_id,
            completed=completed,
            still_running=not completed and session.alive(),
            exit_code=pending.exit_code if completed else None,
            completion_source=pending.completion_source if completed else None,
            output=_redact(data.decode("utf-8", errors="replace"), self.settings),
            cursor=effective,
            next_cursor=next_cursor,
            truncated_before_cursor=truncated,
        )

    async def status(self, terminal_id: str) -> TerminalInfo:
        session = self._session(terminal_id)
        if session.pending is not None and session.pending.completed:
            session.pending = None
        return session.info()

    async def list(self) -> TerminalListResult:
        async with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            if session.pending is not None and session.pending.completed:
                session.pending = None
        return TerminalListResult(terminals=[session.info() for session in sessions])

    async def screen_snapshot(self, terminal_id: str) -> TerminalScreenResult:
        session = self._session(terminal_id)
        with session.condition:
            display = "\n".join(session.screen.display if session.screen is not None else [])
            cursor = session.buffer_end
            cols = session.cols
            rows = session.rows
        return TerminalScreenResult(
            terminal_id=terminal_id,
            columns=cols,
            rows=rows,
            display=_redact(display, self.settings),
            cursor=cursor,
            alive=session.alive(),
            busy=session.pending is not None and not session.pending.completed,
        )

    async def close(self, terminal_id: str) -> TerminalActionResult:
        terminal_id = self._validate_id(terminal_id)
        async with self._lock:
            session = self._sessions.pop(terminal_id, None)
            no_sessions_left = not self._sessions
        if session is None:
            raise ValueError("Unknown terminal_id")
        session.closed = True
        session.stop_reader.set()
        if session.pending_task is not None and not session.pending_task.done():
            session.pending_task.cancel()

        # If an external job currently owns the PTY, terminate that exact process
        # group before closing the shell so terminal_close cannot strand a child.
        foreground = session.foreground_pgid()
        shell_pgid = session.shell_pgid()
        if foreground is not None and foreground != shell_pgid and foreground != os.getpgrp():
            try:
                os.killpg(foreground, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            await asyncio.sleep(0.05)

        try:
            await asyncio.to_thread(session.child.close, True)
        except Exception:
            try:
                session.child.close(force=True)
            except Exception:
                pass
        if session.reader_thread is not None:
            session.reader_thread.join(timeout=1)
        shutil.rmtree(session.state_dir, ignore_errors=True)
        with session.condition:
            session.condition.notify_all()

        # Do not leave a sleeping janitor task pending after the last terminal closes.
        janitor = self._janitor_task
        current = asyncio.current_task()
        if no_sessions_left and janitor is not None and janitor is not current and not janitor.done():
            janitor.cancel()
            try:
                await janitor
            except asyncio.CancelledError:
                pass
            self._janitor_task = None
        return TerminalActionResult(success=True, terminal_id=terminal_id, action="close", alive=False)
