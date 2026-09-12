#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export RMCP_ROOT="$ROOT"
source "$ROOT/scripts/lib.sh"
cd "$ROOT"
ensure_dirs

header "Remote Host MCP $(project_version) - Cloudflare Tunnel"
[[ -f .env && -x .venv/bin/python ]] || die 'Local MCP is not bootstrapped.'

host="${CFD_HOST:-}"
token="${CFD_TOKEN:-}"
reuse_token=false
[[ "${1:-}" == '--reuse-token' ]] && reuse_token=true

step '1/8' 'Validating hostname'
[[ -n "$host" ]] || { fail; die "Missing CFD_HOST."; }
host="${host,,}"; host="${host%.}"
valid_hostname "$host" && ok "$host" || { fail "$host"; die 'Use a DNS hostname only; do not include https:// or /path.'; }

old_env_backup="$(backup_file .env env-before-cft 2>/dev/null || true)"
token_file="$RMCP_SECRET_DIR/cloudflared.token"
had_old_token=false; old_token_backup=''
if [[ -s "$token_file" ]]; then had_old_token=true; old_token_backup="$(backup_file "$token_file" cloudflared-token-before-cft 2>/dev/null || true)"; fi

step '2/8' 'Preparing Tunnel token'
if [[ "$reuse_token" == true || -z "$token" ]]; then
  [[ -s "$token_file" ]] && skip 'reusing saved token file' || { fail; die 'No saved token.'; }
else
  umask 077; printf '%s\n' "$token" > "$token_file"; chmod 600 "$token_file"; unset token CFD_TOKEN; ok 'saved to protected token file'
fi

step '3/8' 'Installing/checking cloudflared'
cf="$(cloudflared_bin 2>/dev/null || true)"; needs_local=false
if [[ -z "$cf" ]]; then needs_local=true
elif ! "$cf" tunnel run --help 2>&1 | grep -q -- '--token-file'; then needs_local=true; fi
if [[ "$needs_local" == true ]]; then
  arch="$(uname -m)"; case "$arch" in x86_64|amd64) cf_arch='amd64' ;; aarch64|arm64) cf_arch='arm64' ;; *) fail "$arch"; die 'Unsupported architecture.' ;; esac
  command -v curl >/dev/null 2>&1 || { fail; die 'curl is required.'; }
  tmp="$(mktemp "$RMCP_RUNTIME_DIR/bin/cloudflared.tmp.XXXXXX")"
  if curl -fL --retry 2 --connect-timeout 10 -o "$tmp" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${cf_arch}" >/dev/null 2>&1; then
    chmod 755 "$tmp"; mv "$tmp" "$RMCP_RUNTIME_DIR/bin/cloudflared"; cf="$RMCP_RUNTIME_DIR/bin/cloudflared"; ok "$("$cf" --version 2>&1 | head -1)"
  else rm -f "$tmp"; fail; die 'Could not download cloudflared.'; fi
else ok "$("$cf" --version 2>&1 | head -1)"; fi

step '4/8' 'Applying MCP public hostname'
set_mcp_env_value PUBLIC_HOST "$host"
if load_env && .venv/bin/python - <<'PY' >/dev/null
from remote_host_mcp.app import load_settings
load_settings()
PY
then ok
else fail; [[ -n "$old_env_backup" ]] && restore_file "$old_env_backup" .env; die 'New configuration failed validation; previous .env restored.'; fi

step '5/8' 'Restarting MCP with new hostname'
if bash scripts/manage.sh restart >/dev/null; then ok
else fail; [[ -n "$old_env_backup" ]] && restore_file "$old_env_backup" .env; bash scripts/manage.sh restart >/dev/null 2>&1 || true; die 'MCP restart failed; previous .env restored.'; fi

step '6/8' 'Starting Cloudflare Tunnel'
export CLOUDFLARED_BIN="$cf"
if bash scripts/manage.sh tunnel-restart >/dev/null; then ok
else
  fail
  if [[ "$had_old_token" == true && -n "$old_token_backup" ]]; then restore_file "$old_token_backup" "$token_file"; elif [[ "$had_old_token" == false ]]; then rm -f "$token_file"; fi
  [[ -n "$old_env_backup" ]] && restore_file "$old_env_backup" .env
  bash scripts/manage.sh restart >/dev/null 2>&1 || true
  bash scripts/manage.sh tunnel-restart >/dev/null 2>&1 || true
  die 'Tunnel did not stay running. Previous config/token were restored when available.'
fi

step '7/8' 'Checking localhost health'
port="$(mcp_env_value PORT 2>/dev/null || printf '8765')"
curl -fsS --max-time 5 "http://127.0.0.1:${port}/health" >/dev/null && ok || { fail; die 'Local MCP health failed after restart.'; }

step '8/8' 'Checking public health'
remote_ok=false
for _ in {1..12}; do if curl -fsS --max-time 8 "https://${host}/health" >/dev/null 2>&1; then remote_ok=true; break; fi; sleep 2; done
if [[ "$remote_ok" == true ]]; then ok "https://${host}/health"; print_final_connection; exit 0; fi

fail "https://${host}/health"
printf '\nTunnel is running but public hostname verification failed.\nLocal MCP remains available at http://127.0.0.1:%s\n' "$port"
exit 2
