"""Sidecar reporting agent: tails the local audit JSONL and ships it to the Hub.

This is 旁路采集 (接入方案 §7.2 option B). The execution server writes the log
and knows nothing about the Hub; this process is what talks to it. Reporting is a
"may fail" feature, so it lives outside the execution path entirely — if the Hub
is down, unreachable or misconfigured, RHMCP keeps serving tool calls.

Delivery contract:

* events are read from the JSONL by byte offset, and the offset only advances
  once the Hub has acknowledged the batch, so a crash or a failed POST replays
  rather than loses;
* batches go out every ``RHMCP_HUB_REPORT_INTERVAL_SECONDS`` or as soon as
  ``RHMCP_HUB_REPORT_BATCH_SIZE`` events have accumulated, capped at
  ``RHMCP_HUB_REPORT_MAX_BATCH`` per request;
* when the Hub is unavailable the reader stops pulling new lines rather than
  dropping them — the on-disk log is the queue.

Run as ``python -m remote_host_mcp.report_agent`` (or the
``remote-host-mcp-report`` console script).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from . import __version__
from .hub_settings import HubConfigError, HubSettings
from .redact import redact_event

logger = logging.getLogger("remote_host_mcp.report")

REPORT_PATH = "/agent/v1/report"
HEARTBEAT_PATH = "/agent/v1/heartbeat"
CONFIG_PATH = "/agent/v1/config"

DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_BACKOFF_SECONDS = 300.0
POLL_SECONDS = 1.0
READ_CHUNK_BYTES = 4 * 1024 * 1024
# Backpressure ceiling: stop reading new lines past this many un-acked events.
MAX_PENDING_EVENTS = 5000

# Fields dropped per report level. `meta` keeps only counters and identity.
_OUTPUT_FIELDS = ("outputBytes", "outputPreview", "outputRef")
_ARG_FIELDS = ("argsText",)

# Required by the Hub; an event missing one of these is dropped, not sent.
_REQUIRED_FIELDS = ("toolName", "status", "startedAt")


@dataclass(slots=True)
class ReportAck:
    ok: bool
    accepted: int = 0
    rejected: int = 0


class HubUnavailable(RuntimeError):
    """The Hub could not be reached or answered with a transport-level failure."""


class HubRejected(RuntimeError):
    """The Hub answered, but refused the request (auth, quota, malformed body)."""


class HubClient:
    """Minimal stdlib HTTP client for the three ``/agent/v1`` endpoints."""

    def __init__(
        self,
        base_url: str,
        agent_key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        opener: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.agent_key = agent_key
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Authorization", f"Bearer {self.agent_key}")
        request.add_header("User-Agent", f"rhmcp-report/{__version__}")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = _safe_body(exc)
            if exc.code in (401, 403):
                raise HubRejected(f"{path} rejected the agent key (HTTP {exc.code})") from exc
            if 400 <= exc.code < 500:
                raise HubRejected(f"{path} returned HTTP {exc.code}: {detail}") from exc
            raise HubUnavailable(f"{path} returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise HubUnavailable(f"{path} unreachable: {type(exc).__name__}") from exc
        if not raw:
            return {}
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HubUnavailable(f"{path} returned a non-JSON body") from exc
        if not isinstance(decoded, dict):
            raise HubUnavailable(f"{path} returned an unexpected body shape")
        return decoded

    def report(self, events: Sequence[Mapping[str, Any]]) -> ReportAck:
        body = self._request("POST", REPORT_PATH, {"events": list(events)})
        return ReportAck(
            ok=bool(body.get("ok", True)),
            accepted=_as_int(body.get("accepted")),
            rejected=_as_int(body.get("rejected")),
        )

    def heartbeat(self) -> dict[str, Any]:
        return self._request("POST", HEARTBEAT_PATH, {"version": __version__})

    def fetch_config(self) -> dict[str, Any]:
        return self._request("GET", CONFIG_PATH)


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _safe_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")[:200]
    except Exception:  # pragma: no cover - diagnostics only
        return ""


def prepare_event(
    event: Mapping[str, Any],
    *,
    report_level: str = "full",
    redact_enabled: bool = True,
) -> dict[str, Any] | None:
    """Trim one audit event to the configured level and redact it.

    Returns ``None`` when the event lacks a Hub-required field, so malformed
    lines are skipped instead of being rejected one batch at a time.
    """
    if any(not event.get(field) for field in _REQUIRED_FIELDS):
        return None
    payload = dict(event)
    if report_level == "meta":
        for field in _ARG_FIELDS + _OUTPUT_FIELDS:
            payload.pop(field, None)
    elif report_level == "tool":
        for field in _OUTPUT_FIELDS:
            payload.pop(field, None)
    return redact_event(payload, enabled=redact_enabled)


class AuditTail:
    """Byte-offset reader over the audit JSONL with an on-disk checkpoint."""

    def __init__(self, log_path: Path, cursor_path: Path) -> None:
        self.log_path = Path(log_path)
        self.cursor_path = Path(cursor_path)
        # `offset` is the acknowledged checkpoint; `read_offset` is how far this
        # process has consumed. Keeping them apart is what lets a failed batch be
        # retried without re-reading (and re-queueing) the same lines.
        self.offset = self._load_offset()
        self.read_offset = self.offset

    def _load_offset(self) -> int:
        try:
            data = json.loads(self.cursor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        offset = data.get("offset") if isinstance(data, dict) else None
        return offset if isinstance(offset, int) and offset >= 0 else 0

    def commit(self, offset: int) -> None:
        """Persist the acknowledged offset atomically."""
        self.offset = offset
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.cursor_path.with_suffix(self.cursor_path.suffix + ".tmp")
        temp.write_text(json.dumps({"offset": offset}), encoding="utf-8")
        os.replace(temp, self.cursor_path)

    def read_new(self) -> Iterator[tuple[int, dict[str, Any]]]:
        """Yield ``(offset_after_line, event)`` pairs for lines not yet read.

        A trailing partial line is left for the next read; a log that shrank
        (rotated or truncated) restarts from the beginning.
        """
        try:
            size = self.log_path.stat().st_size
        except OSError:
            return
        if size < self.read_offset:
            logger.warning("audit log shrank below the read cursor; restarting from 0")
            self.offset = 0
            self.read_offset = 0
        if size == self.read_offset:
            return
        try:
            with self.log_path.open("rb") as handle:
                handle.seek(self.read_offset)
                chunk = handle.read(READ_CHUNK_BYTES)
        except OSError as exc:
            logger.warning("cannot read audit log: %s", type(exc).__name__)
            return
        if not chunk:
            return
        lines = chunk.split(b"\n")
        # The final element is either the empty string left by the trailing
        # newline or an incomplete line still being written; neither is a record.
        lines.pop()
        for line in lines:
            self.read_offset += len(line) + 1
            if not line.strip():
                continue
            try:
                event = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                logger.warning("skipping malformed audit line")
                continue
            if isinstance(event, dict):
                yield self.read_offset, event


class ReportAgent:
    """Reads the audit log, batches events, and ships them with retry."""

    def __init__(
        self,
        settings: HubSettings,
        client: HubClient,
        *,
        log_path: Path,
        cursor_path: Path,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self.settings = settings
        self.client = client
        self.tail = AuditTail(log_path, cursor_path)
        self.poll_seconds = poll_seconds
        self.pending: list[tuple[int, dict[str, Any]]] = []
        self.backoff = 0.0
        self.next_attempt = 0.0
        self.last_flush = 0.0
        self.dropped = 0
        # Effective report level / redaction switch; refreshed by P5 config pull.
        self.report_level = settings.report_level
        self.redact_enabled = settings.redact_enabled

    @property
    def pending_count(self) -> int:
        return len(self.pending)

    def _read_more(self) -> int:
        """Pull new events until the pending ceiling is reached."""
        added = 0
        if len(self.pending) >= MAX_PENDING_EVENTS:
            logger.warning(
                "pending buffer at ceiling (%d); pausing reads until the Hub acknowledges",
                MAX_PENDING_EVENTS,
            )
            return 0
        for offset, raw in self.tail.read_new():
            prepared = prepare_event(
                raw, report_level=self.report_level, redact_enabled=self.redact_enabled
            )
            if prepared is None:
                # Unusable line: the read cursor has already moved past it, so it
                # cannot block the queue. It is re-read (and re-dropped) only if
                # the process restarts before anything is acknowledged.
                self.dropped += 1
                continue
            self.pending.append((offset, prepared))
            added += 1
            if len(self.pending) >= MAX_PENDING_EVENTS:
                break
        return added

    def _should_flush(self, now: float) -> bool:
        if not self.pending:
            return False
        if len(self.pending) >= self.settings.report_batch_size:
            return True
        return now >= self.last_flush + self.settings.report_interval_seconds

    def flush(self, now: float | None = None) -> bool:
        """Send one batch. Returns True when the Hub acknowledged it."""
        moment = time.monotonic() if now is None else now
        if not self.pending:
            return True
        batch = self.pending[: self.settings.report_max_batch]
        try:
            ack = self.client.report([event for _, event in batch])
        except HubUnavailable as exc:
            self._schedule_retry(str(exc), moment)
            return False
        except HubRejected as exc:
            # A rejected request will never succeed on replay; dropping the batch
            # is the only way to keep the queue moving. Log loudly.
            logger.error("Hub rejected the report batch, dropping %d events: %s", len(batch), exc)
            self._commit(batch, moment)
            self._reset_backoff()
            return False
        if not ack.ok:
            self._schedule_retry("Hub returned ok=false", moment)
            return False
        if ack.rejected:
            logger.warning("Hub rejected %d of %d events in the batch", ack.rejected, len(batch))
        self._commit(batch, moment)
        self._reset_backoff()
        return True

    def _commit(self, batch: Sequence[tuple[int, dict[str, Any]]], now: float) -> None:
        del self.pending[: len(batch)]
        self.tail.commit(batch[-1][0])
        self.last_flush = now

    def _schedule_retry(self, reason: str, now: float) -> None:
        self.backoff = min(
            self.backoff * 2 if self.backoff else self.settings.report_interval_seconds,
            MAX_BACKOFF_SECONDS,
        )
        self.next_attempt = now + self.backoff
        logger.warning("report batch not acknowledged (%s); retrying in %.0fs", reason, self.backoff)

    def _reset_backoff(self) -> None:
        self.backoff = 0.0
        self.next_attempt = 0.0

    def cycle(self, now: float | None = None) -> bool:
        """One poll: read new events, then flush while a batch is due."""
        moment = time.monotonic() if now is None else now
        if moment >= self.next_attempt:
            self._read_more()
            if self._should_flush(moment):
                while self.pending and moment >= self.next_attempt:
                    if not self.flush(moment):
                        break
                    moment = time.monotonic() if now is None else now
        return bool(self.pending)

    def run(self) -> None:
        logger.info(
            "reporting to %s as host %s (level=%s redact=%s batch=%d/%ds)",
            self.settings.base_url,
            self.settings.host_id,
            self.report_level,
            self.redact_enabled,
            self.settings.report_batch_size,
            self.settings.report_interval_seconds,
        )
        while True:
            try:
                self.cycle()
            except Exception as exc:  # a sidecar must survive its own bugs
                logger.exception("report cycle failed: %s", type(exc).__name__)
            time.sleep(self.poll_seconds)


def build_agent(settings: HubSettings, *, state_dir: Path | None = None) -> ReportAgent:
    if not settings.configured:
        raise HubConfigError(
            "RHMCP_HUB_BASE_URL, RHMCP_HUB_HOST_ID and RHMCP_HUB_AGENT_KEY must all be set"
        )
    assert settings.base_url and settings.agent_key  # guaranteed by `configured`
    log_path = settings.resolved_audit_log_path()
    cursor_dir = Path(state_dir) if state_dir is not None else log_path.parent
    return ReportAgent(
        settings,
        HubClient(settings.base_url, settings.agent_key),
        log_path=log_path,
        cursor_path=cursor_dir / "report.cursor",
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Ship RHMCP call records to CF-MCP-HUB.")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit (verification aid)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    try:
        settings = HubSettings.from_env(strict=True)
        agent = build_agent(settings)
    except HubConfigError as exc:
        raise SystemExit(f"Hub configuration error: {exc}") from exc

    if not settings.reporting_active:
        raise SystemExit("reporting is disabled (RHMCP_HUB_REPORT_ENABLED=false)")
    if args.once:
        agent.cycle()
        return
    try:
        agent.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
