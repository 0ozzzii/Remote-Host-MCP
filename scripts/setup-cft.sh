#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export RMCP_ROOT="$ROOT"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib.sh"
# shellcheck disable=SC1091
source "$ROOT/installer/lib/cloudflare.sh"
cd "$ROOT"
ensure_dirs
VERSION="$(tr -d '\r\n' < "$ROOT/VERSION" 2>/dev/null || printf 'unknown')"

header "Remote Host MCP $VERSION - Step 2/2: Cloudflare Tunnel"

[[ -f .env && -x .venv/bin/python ]] || die 'Local MCP is not bootstrapped. Run: bash scripts/bootstrap.sh'

host="${CFD_HOST:-}"
token="${CFD_TOKEN:-}"
reuse_token=false
[[ "${1:-}" == '--reuse-token' ]] && reuse_token=true

step '1/8' 'Validating hostname'
if [[ -z "$host" ]]; then
  fail
  die "Missing CFD_HOST. Example: CFD_HOST='mcp.example.com' CFD_TOKEN='...' bash scripts/setup-cft.sh"
fi
host="${host,,}"
host="${host%.}"
if valid_hostname "$host"; then
  ok "$host"
else
  fail "$host"
  die 'Use a DNS hostname only; do not include https:// or /path.'
fi

old_env_backup="$(backup_file .env env-before-cft 2>/dev/null || true)"
token_file="$RMCP_SECRET_DIR/cloudflared.token"
had_old_token=false
old_token_backup=""
if [[ -s "$token_file" ]]; then
  had_old_token=true
  old_token_backup="$(backup_file "$token_file" cloudflared-token-before-cft 2>/dev/null || true)"
fi

step '2/8' 'Preparing Tunnel token'
if [[ "$reuse_token" == true || -z "$token" ]]; then
  if [[ -s "$token_file" ]]; then
    skip 'reusing saved token file'
  else
    fail
    die "No saved token. Re-run with CFD_TOKEN='PASTE_TUNNEL_TOKEN_HERE'."
  fi
else
  umask 077
  printf '%s\n' "$token" > "$token_file"
  chmod 600 "$token_file"
  unset token CFD_TOKEN
  ok 'saved to protected token file'
fi

step '3/8' 'Installing/checking cloudflared'
cf="$(cloudflared_bin 2>/dev/null || true)"
needs_local=false
if [[ -z "$cf" ]]; then
  needs_local=true
elif ! "$cf" tunnel run --help 2>&1 | grep -q -- '--token-file'; then
  needs_local=true
fi

if [[ "$needs_local" == true ]]; then
  cf="$(install_pinned_cloudflared "$RMCP_RUNTIME_DIR")" || {
    fail
    die "Could not install verified cloudflared ${CLOUDFLARED_PINNED_VERSION}."
  }
  ok "$("$cf" --version 2>&1 | head -1)"
else
  ok "$("$cf" --version 2>&1 | head -1)"
fi

step '4/8' 'Applying MCP public hostname'
set_mcp_env_value PUBLIC_HOST "$host"
if load_env && .venv/bin/python - <<'PY' >/dev/null
from remote_host_mcp.app import load_settings
load_settings()
PY
then
  ok
else
  fail
  [[ -n "$old_env_backup" ]] && restore_file "$old_env_backup" .env
  die 'New configuration failed validation; previous .env restored.'
fi

step '5/8' 'Restarting MCP with new hostname'
if bash scripts/manage.sh restart >/dev/null; then
  ok
else
  fail
  [[ -n "$old_env_backup" ]] && restore_file "$old_env_backup" .env
  bash scripts/manage.sh restart >/dev/null 2>&1 || true
  die 'MCP restart failed; previous .env restored.'
fi

step '6/8' 'Starting Cloudflare Tunnel'
export CLOUDFLARED_BIN="$cf"
if bash scripts/manage.sh tunnel-restart >/dev/null; then
  ok
else
  fail
  if [[ "$had_old_token" == true && -n "$old_token_backup" ]]; then
    restore_file "$old_token_backup" "$token_file"
  elif [[ "$had_old_token" == false ]]; then
    rm -f "$token_file"
  fi
  [[ -n "$old_env_backup" ]] && restore_file "$old_env_backup" .env
  bash scripts/manage.sh restart >/dev/null 2>&1 || true
  bash scripts/manage.sh tunnel-restart >/dev/null 2>&1 || true
  die 'Tunnel did not stay running. Previous config/token were restored when available.'
fi

step '7/8' 'Checking localhost health'
port="$(mcp_env_value PORT 2>/dev/null || printf '8765')"
if curl -fsS --max-time 5 "http://127.0.0.1:${port}/health" >/dev/null; then
  ok
else
  fail
  die 'Local MCP health failed after restart.'
fi

step '8/8' 'Checking public health'
remote_ok=false
for _ in {1..12}; do
  if curl -fsS --max-time 8 "https://${host}/health" >/dev/null 2>&1; then
    remote_ok=true
    break
  fi
  sleep 2
done

if [[ "$remote_ok" == true ]]; then
  ok "https://${host}/health"
  print_final_connection
  exit 0
fi

fail "https://${host}/health"
cat <<EOF

Cloudflare Tunnel appears to be running, but the public hostname did not pass health verification.

Local MCP: OK
Configured hostname: $host
Expected Cloudflare service/origin: http://127.0.0.1:${port}

Nothing was deleted. Backups were created before changing configuration.
EOF

if [[ -t 0 ]]; then
  subhr
  printf 'Choose what to do now:\n'
  printf '  1. Keep the new configuration and fix Cloudflare later\n'
  printf '  2. Restore the previous hostname/token now\n'
  printf '  0. Exit without further changes\n'
  read -r -p 'Select [0-2]: ' choice
  case "$choice" in
    2)
      [[ -n "$old_env_backup" ]] && restore_file "$old_env_backup" .env
      if [[ "$had_old_token" == true && -n "$old_token_backup" ]]; then
        restore_file "$old_token_backup" "$token_file"
      elif [[ "$had_old_token" == false ]]; then
        rm -f "$token_file"
      fi
      bash scripts/manage.sh restart >/dev/null 2>&1 || true
      bash scripts/manage.sh tunnel-restart >/dev/null 2>&1 || true
      info 'Previous configuration restored.'
      ;;
    1) info 'New configuration kept. Use rmcp to diagnose or change it.' ;;
    *) info 'No further changes made.' ;;
  esac
fi

printf '\nRun: ./rmcp\n'
exit 2
