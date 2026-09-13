#!/usr/bin/env bash
set -euo pipefail

port_free() {
  local port="$1"
  [[ "$port" =~ ^[0-9]+$ && "$port" -ge 1024 && "$port" -le 65535 ]] || return 2
  python3 - "$port" <<'PY'
import socket, sys
p=int(sys.argv[1])
s=socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    # Match normal asyncio/HTTP server restart semantics: SO_REUSEADDR permits
    # rebinding after a clean shutdown while an active listener still fails.
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('127.0.0.1', p))
except OSError:
    raise SystemExit(1)
finally:
    s.close()
PY
}

port_listening() {
  local port="$1"
  [[ "$port" =~ ^[0-9]+$ && "$port" -ge 1 && "$port" -le 65535 ]] || return 2
  python3 - "$port" <<'PY'
import socket, sys
p=int(sys.argv[1])
s=socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(0.25)
try:
    rc=s.connect_ex(('127.0.0.1', p))
finally:
    s.close()
raise SystemExit(0 if rc == 0 else 1)
PY
}

port_owner() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | awk -v p=":${port}" '$4 ~ p"$" {print; exit}'
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | tail -n +2 | head -1
  fi
}

next_free_port() {
  local start="${1:-8765}" p
  for ((p=start; p<=start+100; p++)); do
    if port_free "$p"; then printf '%s\n' "$p"; return 0; fi
  done
  return 1
}

choose_port() {
  local preferred="${1:-8765}" choice custom candidate owner
  if port_free "$preferred"; then
    ok "$(t port_ok): $preferred"
    CHOSEN_PORT="$preferred"
    export CHOSEN_PORT
    return 0
  fi
  owner="$(port_owner "$preferred" || true)"
  warn "$(t port_busy): $preferred${owner:+ — $owner}"
  while true; do
    printf '  1. %s\n' "$(t port_auto)"
    printf '  2. %s\n' "$(t port_custom)"
    printf '  0. %s\n' 'Exit / 退出'
    read -r -p 'Select / 选择 [0-2]: ' choice
    case "$choice" in
      1)
        candidate="$(next_free_port "$((preferred+1))")" || die 'No free port found / 未找到可用端口'
        CHOSEN_PORT="$candidate"
        export CHOSEN_PORT
        ok "Port / 端口: $CHOSEN_PORT"
        return 0
        ;;
      2)
        read -r -p 'Port / 端口: ' custom
        if port_free "$custom"; then CHOSEN_PORT="$custom"; export CHOSEN_PORT; ok "Port / 端口: $CHOSEN_PORT"; return 0; fi
        warn "Port unavailable / 端口不可用: $custom"
        ;;
      0) exit 0 ;;
      *) warn 'Invalid selection / 无效选项' ;;
    esac
  done
}