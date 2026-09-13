from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request

PROTOCOL = "2026-07-28"
EXPECTED_TOOLS = int(os.getenv("RHMCP_VALIDATE_TOOL_COUNT", "65"))
URL = os.environ.get("RHMCP_VALIDATE_URL", "")
BEARER = os.environ.get("RHMCP_VALIDATION_BEARER_TOKEN", "")
CLIENT_INFO = {"name": "remote-host-mcp-installer", "version": "1"}
CLIENT_CAPABILITIES: dict = {}

if not URL.startswith(("http://127.0.0.1:", "https://")):
    raise SystemExit("validator URL is missing or unsafe")

session_id: str | None = None


def _request_meta() -> dict:
    """Return the modern MCP envelope required by Remote Host MCP."""
    return {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL,
        "io.modelcontextprotocol/clientCapabilities": CLIENT_CAPABILITIES,
        "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
    }


def _redact(text: str) -> str:
    text = re.sub(r"(https?://[^/\s]+/mcp/)[A-Za-z0-9._~+/=-]+", r"\1<redacted>", text)
    text = re.sub(r"(?i)(token|key|secret)([= :]+)[^\s,}\]]+", r"\1\2<redacted>", text)
    if BEARER:
        text = text.replace(BEARER, "<redacted>")
    return text[:300]


def _decode_body(raw: bytes) -> dict:
    text = raw.decode("utf-8", errors="strict").strip()
    if not text:
        return {}
    if text.startswith("{"):
        return json.loads(text)
    data_lines = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
    if not data_lines:
        raise ValueError("no JSON or SSE data payload")
    return json.loads(data_lines[-1])


def rpc(method: str, params: dict | None = None, *, request_id: int | None = 1, tool_name: str | None = None) -> dict:
    global session_id
    request_params = dict(params or {})
    request_params["_meta"] = _request_meta()
    payload: dict = {"jsonrpc": "2.0", "method": method, "params": request_params}
    if request_id is not None:
        payload["id"] = request_id
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL,
        "Mcp-Method": method,
    }
    if tool_name:
        headers["Mcp-Name"] = tool_name
    if BEARER:
        headers["Authorization"] = f"Bearer {BEARER}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15, context=ssl.create_default_context()) as resp:
            new_session = resp.headers.get("Mcp-Session-Id")
            if new_session:
                session_id = new_session
            raw = resp.read()
            if request_id is None:
                return {}
            doc = _decode_body(raw)
    except urllib.error.HTTPError as exc:
        safe = _redact(exc.read(2048).decode("utf-8", errors="replace"))
        raise RuntimeError(f"HTTP {exc.code}: {safe}") from None
    if "error" in doc:
        raise RuntimeError(f"RPC error: {_redact(json.dumps(doc['error'], ensure_ascii=False))}")
    return doc.get("result", {})


init = rpc(
    "initialize",
    {
        "protocolVersion": PROTOCOL,
        "capabilities": CLIENT_CAPABILITIES,
        "clientInfo": CLIENT_INFO,
    },
    request_id=1,
)
server_info = init.get("serverInfo") or {}
if server_info.get("name") != "Remote Host MCP":
    raise SystemExit(f"unexpected serverInfo: {server_info!r}")

try:
    rpc("notifications/initialized", {}, request_id=None)
except Exception:
    # Stateless servers may not emit a response for notifications.
    pass

tools_result = rpc("tools/list", {}, request_id=2)
tools = tools_result.get("tools") or []
names = {tool.get("name") for tool in tools}
if len(tools) != EXPECTED_TOOLS:
    raise SystemExit(f"tool count mismatch: expected {EXPECTED_TOOLS}, got {len(tools)}")
required = {"status", "host_capabilities"}
missing = required - names
if missing:
    raise SystemExit(f"missing required tools: {sorted(missing)}")

cap = rpc("tools/call", {"name": "host_capabilities", "arguments": {}}, request_id=3, tool_name="host_capabilities")
structured = cap.get("structuredContent") or cap.get("structured_content") or {}
if not structured:
    raise SystemExit("host_capabilities returned no structured content")

status = rpc("tools/call", {"name": "status", "arguments": {}}, request_id=4, tool_name="status")
status_structured = status.get("structuredContent") or status.get("structured_content") or {}
if status_structured.get("service") != "Remote Host MCP":
    raise SystemExit("status tool did not identify Remote Host MCP")

print(f"MCP_FINAL_GATE_PASS tools={len(tools)} server={server_info.get('name')}")
