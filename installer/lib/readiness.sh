#!/usr/bin/env bash
set -euo pipefail

wait_local_health() {
  local port="$1" timeout_s="${2:-30}" interval_s="${3:-1}" elapsed=0
  while (( elapsed < timeout_s )); do
    if curl -fsS --max-time 3 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$interval_s"
    elapsed=$((elapsed + interval_s))
  done
  return 1
}

service_readiness_diagnostics() {
  local port="$1"
  fail "Remote Host MCP did not become ready on 127.0.0.1:${port}. / Remote Host MCP 未在限定时间内就绪。"
  if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
    printf '%s\n' '--- systemctl status ---' >&2
    systemctl status remote-host-mcp.service --no-pager -l 2>&1 | tail -n 50 >&2 || true
    printf '%s\n' '--- journalctl ---' >&2
    journalctl -u remote-host-mcp.service -n 80 --no-pager 2>&1 | sed -E \
      -e 's#(https?://[^/[:space:]]+/mcp/)[A-Za-z0-9._~+/=-]+#\1<redacted>#g' \
      -e 's#([Tt]oken|[Kk]ey|[Ss]ecret)([= :]+)[^[:space:]]+#\1\2<redacted>#g' >&2 || true
  fi
  printf '%s\n' '--- listener ---' >&2
  if command -v ss >/dev/null 2>&1; then ss -ltnp 2>/dev/null | awk -v p=":${port}" '$4 ~ p"$" {print}' >&2 || true; fi
  printf '%s\n' '--- health probe ---' >&2
  curl -sS -D - --max-time 3 "http://127.0.0.1:${port}/health" -o /dev/null >&2 || true
}

wait_public_https() {
  local host="$1" port="${2:-443}" timeout_s="${3:-30}" elapsed=0 url
  if [[ "$port" == 443 ]]; then url="https://${host}/health"; else url="https://${host}:${port}/health"; fi
  while (( elapsed < timeout_s )); do
    if curl -fsS --max-time 5 "$url" >/dev/null 2>&1; then return 0; fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  return 1
}
