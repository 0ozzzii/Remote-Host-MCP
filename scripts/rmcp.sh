#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export RMCP_ROOT="$ROOT"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib.sh"
cd "$ROOT"
ensure_dirs

case "${1:-}" in
  --root)
    printf '%s\n' "$ROOT"
    exit 0
    ;;
  --version|-V)
    printf '%s\n' 'Remote Host MCP 0.1.0-alpha.1'
    exit 0
    ;;
  --help|-h)
    cat <<'EOF'
Usage:
  rmcp              Open the interactive management menu
  rmcp --root       Print the active Remote Host MCP project root
  rmcp --version    Print the management CLI version
EOF
    exit 0
    ;;
esac

pause() {
  printf '\n'
  read -r -p 'Press Enter to continue...' _ || true
}

show_status() {
  load_env || true
  local server='DOWN' tunnel='DOWN' public='NOT CONFIGURED'
  local host port
  host="$(mcp_env_value PUBLIC_HOST 2>/dev/null || printf 'not-configured')"
  port="$(mcp_env_value PORT 2>/dev/null || printf '8765')"

  pid_alive logs/server.pid && server='RUNNING'
  pid_alive logs/tunnel.pid && tunnel='RUNNING'

  if [[ "$host" != 'not-configured' && "$host" != 'mcp.invalid' ]] &&
     curl -fsS --max-time 4 "https://${host}/health" >/dev/null 2>&1; then
    public='OK'
  fi

  header 'Remote Host MCP 0.1.0-alpha.1 - Management'
  printf 'MCP Server      : %s\n' "$server"
  printf 'Cloudflare CFT  : %s\n' "$tunnel"
  printf 'Public Health   : %s\n' "$public"
  printf 'Hostname        : %s\n' "$host"
  printf 'Local endpoint  : http://127.0.0.1:%s\n' "$port"
  subhr
}

change_host() {
  load_env || die 'Run bootstrap first.'
  local current new port
  current="$(mcp_env_value PUBLIC_HOST 2>/dev/null || printf 'mcp.invalid')"
  port="$(mcp_env_value PORT 2>/dev/null || printf '8765')"
  printf 'Current hostname: %s\n' "$current"
  read -r -p 'New hostname (no https://, no path): ' new
  new="${new,,}"
  new="${new%.}"

  valid_hostname "$new" || {
    warn 'Invalid hostname. Nothing changed.'
    return
  }

  printf '\nCloudflare must also route this hostname to http://127.0.0.1:%s\n' "$port"
  read -r -p "Apply '$new'? [y/N]: " yn
  [[ "$yn" =~ ^[Yy]$ ]] || {
    info 'Cancelled.'
    return
  }

  CFD_HOST="$new" bash scripts/setup-cft.sh --reuse-token || true
}

change_token() {
  load_env || die 'Run bootstrap first.'
  local host
  host="$(mcp_env_value PUBLIC_HOST 2>/dev/null || printf 'mcp.invalid')"
  [[ "$host" == 'mcp.invalid' ]] && host='YOUR_MCP_DOMAIN'
  cat <<EOF
Copy the command below, replace the token placeholder, then run it:

CFD_HOST='$host' CFD_TOKEN='PASTE_TUNNEL_TOKEN_HERE' bash scripts/setup-cft.sh
EOF
}

rotate_key() {
  load_env || die 'Run bootstrap first.'
  printf 'WARNING: Rotating the MCP Path Key immediately invalidates the old MCP capability URL.\n'
  read -r -p 'Continue? [y/N]: ' yn
  [[ "$yn" =~ ^[Yy]$ ]] || {
    info 'Cancelled.'
    return
  }

  backup="$(backup_file .env env-before-key-rotation)"
  new="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"
  set_mcp_env_value PATH_KEY "$new"

  if bash scripts/manage.sh restart >/dev/null; then
    info "Key rotated. Backup: $backup"
    print_final_connection
  else
    restore_file "$backup" .env
    bash scripts/manage.sh restart >/dev/null 2>&1 || true
    warn 'Restart failed; previous key restored.'
  fi
}

full_diag() {
  load_env || true
  local port host
  port="$(mcp_env_value PORT 2>/dev/null || printf '8765')"
  host="$(mcp_env_value PUBLIC_HOST 2>/dev/null || printf 'mcp.invalid')"
  header 'Remote Host MCP 0.1.0-alpha.1 - Full diagnostics'

  step '1/5' 'MCP process'
  if pid_alive logs/server.pid; then ok "PID $(cat logs/server.pid)"; else fail 'DOWN'; fi

  step '2/5' 'Local health'
  if curl -fsS --max-time 5 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then ok; else fail; fi

  step '3/5' 'Tunnel process'
  if pid_alive logs/tunnel.pid; then ok "PID $(cat logs/tunnel.pid)"; else fail 'DOWN'; fi

  step '4/5' 'Public hostname config'
  if [[ "$host" != 'mcp.invalid' ]]; then ok "$host"; else fail 'not configured'; fi

  step '5/5' 'Public health'
  if [[ "$host" != 'mcp.invalid' ]] && curl -fsS --max-time 8 "https://${host}/health" >/dev/null 2>&1; then
    ok
  else
    fail
  fi

  subhr
  printf 'If local health fails: bash scripts/manage.sh logs 120\n'
  printf 'If Tunnel fails:      bash scripts/manage.sh tunnel-logs 120\n'
}

while true; do
  clear 2>/dev/null || true
  show_status
  cat <<'EOF'
 1. Install / repair local MCP
 2. Configure Cloudflare Tunnel (show setup command)
 3. Change public hostname (reuse saved Tunnel token)
 4. Replace Cloudflare Tunnel token (show command template)
 5. Regenerate MCP Path Key
 6. Show MCP Path Key and full MCP URL
 7. Full diagnostics
 8. Restart MCP server
 9. Restart Cloudflare Tunnel
10. View MCP logs
11. View Cloudflare Tunnel logs
12. Stop MCP server
13. Stop Cloudflare Tunnel
14. Repair / re-register global rmcp command
 0. Exit
EOF
  subhr
  read -r -p 'Select [0-14]: ' choice

  case "$choice" in
    1) bash scripts/bootstrap.sh; pause ;;
    2)
      load_env || true
      menu_host="$(mcp_env_value PUBLIC_HOST 2>/dev/null || printf 'YOUR_MCP_DOMAIN')"
      [[ "$menu_host" == "mcp.invalid" ]] && menu_host="YOUR_MCP_DOMAIN"
      print_next_cft_command "$menu_host"
      pause
      ;;
    3) change_host; pause ;;
    4) change_token; pause ;;
    5) rotate_key; pause ;;
    6) print_final_connection; pause ;;
    7) full_diag; pause ;;
    8) bash scripts/manage.sh restart; pause ;;
    9) bash scripts/manage.sh tunnel-restart; pause ;;
    10) bash scripts/manage.sh logs 120; pause ;;
    11) bash scripts/manage.sh tunnel-logs 120; pause ;;
    12) bash scripts/manage.sh stop; pause ;;
    13) bash scripts/manage.sh tunnel-stop; pause ;;
    14) bash scripts/register-command.sh; pause ;;
    0) exit 0 ;;
    *) warn 'Invalid selection.'; sleep 1 ;;
  esac
done
