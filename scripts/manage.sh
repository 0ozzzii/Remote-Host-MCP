#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export RMCP_ROOT="$ROOT"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib.sh"
cd "$ROOT"
ensure_dirs

load_required_env() {
  load_env || die "Missing $ROOT/.env. Run: bash scripts/bootstrap.sh"
}

proc_start_ticks() {
  local pid="$1" stat rest
  [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/stat" ]] || return 1
  stat="$(cat "/proc/$pid/stat" 2>/dev/null)" || return 1
  rest="${stat##*) }"
  local -a fields=()
  read -r -a fields <<< "$rest"
  [[ "${#fields[@]}" -ge 20 ]] || return 1
  printf '%s\n' "${fields[19]}"
}

pid_cmdline() {
  local pid="$1"
  [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/cmdline" ]] || return 1
  tr '\0' ' ' < "/proc/$pid/cmdline"
}

write_pid_identity() {
  local pidfile="$1" pid="$2" ticks
  ticks="$(proc_start_ticks "$pid")" || return 1
  printf '%s\n' "$pid" > "$pidfile"
  printf '%s\n' "$ticks" > "${pidfile}.start_ticks"
}

pid_identity_alive() {
  local pidfile="$1" needle="$2" pid expected_ticks current_ticks cmdline
  [[ -f "$pidfile" ]] || return 1
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  current_ticks="$(proc_start_ticks "$pid" 2>/dev/null || true)"
  [[ "$current_ticks" =~ ^[0-9]+$ ]] || return 1
  if [[ -f "${pidfile}.start_ticks" ]]; then
    expected_ticks="$(cat "${pidfile}.start_ticks" 2>/dev/null || true)"
    [[ "$expected_ticks" =~ ^[0-9]+$ && "$expected_ticks" == "$current_ticks" ]] || return 1
  fi
  cmdline="$(pid_cmdline "$pid" 2>/dev/null || true)"
  [[ -n "$cmdline" && "$cmdline" == *"$needle"* ]]
}

clear_pid_identity() {
  local pidfile="$1"
  rm -f "$pidfile" "${pidfile}.start_ticks"
}

stop_pidfile() {
  local pidfile="$1" label="$2" needle="$3" pid
  if [[ -f "$pidfile" ]]; then
    pid="$(cat "$pidfile" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
      if ! pid_identity_alive "$pidfile" "$needle"; then
        die "Refusing to stop $label: PID $pid does not match the recorded process identity."
      fi
      kill "$pid" 2>/dev/null || true
      for _ in {1..30}; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1
      done
      if kill -0 "$pid" 2>/dev/null; then
        pid_identity_alive "$pidfile" "$needle" ||
          die "Refusing SIGKILL for $label: process identity changed after TERM."
        kill -9 "$pid" 2>/dev/null || true
      fi
    fi
  fi
  clear_pid_identity "$pidfile"
  printf '%s stopped\n' "$label"
}

server_bin() {
  if [[ -x .venv/bin/remote-host-mcp ]]; then
    printf '%s\n' "$ROOT/.venv/bin/remote-host-mcp"
  elif [[ -x .venv/bin/dsw-direct-mcp ]]; then
    printf '%s\n' "$ROOT/.venv/bin/dsw-direct-mcp"
  else
    return 1
  fi
}

cmd="${1:-help}"
case "$cmd" in
  install)
    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/pip install -e '.[dev]'
    if [[ ! -f .env ]]; then
      cp .env.example .env
      chmod 600 .env
      echo 'Created .env.'
    fi
    .venv/bin/pytest -q
    ;;
  start)
    load_required_env
    server_exec="$(server_bin 2>/dev/null || true)"
    [[ -n "$server_exec" ]] || die 'Run: bash scripts/bootstrap.sh'
    if pid_identity_alive logs/server.pid "$server_exec"; then
      echo "server already running: PID $(cat logs/server.pid)"
      exit 0
    fi
    if [[ -f logs/server.pid ]]; then
      stale_pid="$(cat logs/server.pid 2>/dev/null || true)"
      if [[ "$stale_pid" =~ ^[0-9]+$ ]] && kill -0 "$stale_pid" 2>/dev/null; then
        die "Refusing to start a second server: logs/server.pid points to live PID $stale_pid with mismatched identity."
      fi
      clear_pid_identity logs/server.pid
    fi
    nohup "$server_exec" >> logs/server.log 2>&1 &
    server_pid=$!
    write_pid_identity logs/server.pid "$server_pid" || {
      kill "$server_pid" 2>/dev/null || true
      die 'Could not record server PID identity.'
    }
    sleep 1
    pid_identity_alive logs/server.pid "$server_exec" || { tail -80 logs/server.log >&2 || true; exit 1; }
    echo "server started: PID $(cat logs/server.pid)"
    ;;
  stop)
    server_exec="$(server_bin 2>/dev/null || printf '%s' "$ROOT/.venv/bin/remote-host-mcp")"
    stop_pidfile logs/server.pid server "$server_exec"
    ;;
  restart)
    bash "$0" stop >/dev/null
    bash "$0" start
    ;;
  tunnel-start)
    load_required_env
    token_file="$RMCP_SECRET_DIR/cloudflared.token"
    [[ -s "$token_file" ]] || die 'Missing secrets/cloudflared.token. Run scripts/setup-cft.sh.'
    cf="${CLOUDFLARED_BIN:-$(cloudflared_bin 2>/dev/null || true)}"
    [[ -n "$cf" && -x "$cf" ]] || die 'cloudflared not found. Run scripts/setup-cft.sh.'
    "$cf" tunnel run --help 2>&1 | grep -q -- '--token-file' ||
      die 'cloudflared is too old; token-file support requires 2025.4.0+.'
    tunnel_needle="$token_file"
    if pid_identity_alive logs/tunnel.pid "$tunnel_needle"; then
      echo "tunnel already running: PID $(cat logs/tunnel.pid)"
      exit 0
    fi
    if [[ -f logs/tunnel.pid ]]; then
      stale_pid="$(cat logs/tunnel.pid 2>/dev/null || true)"
      if [[ "$stale_pid" =~ ^[0-9]+$ ]] && kill -0 "$stale_pid" 2>/dev/null; then
        die "Refusing to start a second tunnel: logs/tunnel.pid points to live PID $stale_pid with mismatched identity."
      fi
      clear_pid_identity logs/tunnel.pid
    fi
    nohup "$cf" tunnel --no-autoupdate run --token-file "$token_file" >> logs/tunnel.log 2>&1 &
    tunnel_pid=$!
    write_pid_identity logs/tunnel.pid "$tunnel_pid" || {
      kill "$tunnel_pid" 2>/dev/null || true
      die 'Could not record tunnel PID identity.'
    }
    sleep 3
    pid_identity_alive logs/tunnel.pid "$tunnel_needle" || { tail -80 logs/tunnel.log >&2 || true; exit 1; }
    echo "tunnel started: PID $(cat logs/tunnel.pid)"
    ;;
  tunnel-stop)
    stop_pidfile logs/tunnel.pid tunnel "$RMCP_SECRET_DIR/cloudflared.token"
    ;;
  tunnel-restart)
    bash "$0" tunnel-stop >/dev/null
    bash "$0" tunnel-start
    ;;
  check)
    load_required_env
    port="$(mcp_env_value PORT 2>/dev/null || printf '8765')"
    host="$(mcp_env_value PUBLIC_HOST 2>/dev/null || true)"
    echo 'Local health:'
    curl -fsS --max-time 5 "http://127.0.0.1:${port}/health"
    echo
    if [[ -z "$host" || "$host" == 'mcp.invalid' ]]; then
      echo 'Remote health: NOT CONFIGURED'
    else
      echo 'Remote health:'
      curl -fsS --max-time 10 "https://${host}/health"
      echo
    fi
    ;;
  status)
    load_env || true
    server_exec="$(server_bin 2>/dev/null || printf '%s' "$ROOT/.venv/bin/remote-host-mcp")"
    if pid_identity_alive logs/server.pid "$server_exec"; then
      echo "server: UP pid=$(cat logs/server.pid)"
    else
      echo 'server: DOWN or stale pidfile'
    fi
    if pid_identity_alive logs/tunnel.pid "$RMCP_SECRET_DIR/cloudflared.token"; then
      echo "tunnel: UP pid=$(cat logs/tunnel.pid)"
    else
      echo 'tunnel: DOWN or stale pidfile'
    fi
    printf 'hostname: %s\n' "$(mcp_env_value PUBLIC_HOST 2>/dev/null || printf 'not-configured')"
    ;;
  logs)
    tail -n "${2:-100}" logs/server.log 2>/dev/null || true
    ;;
  tunnel-logs)
    tail -n "${2:-100}" logs/tunnel.log 2>/dev/null || true
    ;;
  *)
    echo "Usage: $0 {install|start|stop|restart|tunnel-start|tunnel-stop|tunnel-restart|check|status|logs|tunnel-logs}"
    ;;
esac
