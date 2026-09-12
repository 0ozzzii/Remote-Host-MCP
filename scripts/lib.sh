#!/usr/bin/env bash
set -euo pipefail

RMCP_ROOT="${RMCP_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
RMCP_ENV="$RMCP_ROOT/.env"
RMCP_LOG_DIR="$RMCP_ROOT/logs"
RMCP_SECRET_DIR="$RMCP_ROOT/secrets"
RMCP_BACKUP_DIR="$RMCP_ROOT/backups"
RMCP_RUNTIME_DIR="$RMCP_ROOT/.runtime"

C_RESET='\033[0m'
C_GREEN='\033[32m'
C_YELLOW='\033[33m'
C_RED='\033[31m'
C_CYAN='\033[36m'

use_color() { [[ -t 1 && "${NO_COLOR:-}" == "" ]]; }
paint() {
  local color="$1"
  shift
  if use_color; then printf '%b%s%b' "$color" "$*" "$C_RESET"; else printf '%s' "$*"; fi
}

hr() { printf '%s\n' '============================================================'; }
subhr() { printf '%s\n' '------------------------------------------------------------'; }
header() { hr; printf ' %s\n' "$1"; hr; }
step() { printf '[%s] %-42s ' "$1" "$2"; }
ok() { paint "$C_GREEN" 'OK'; printf '%s\n' "${1:+  $1}"; }
skip() { paint "$C_CYAN" 'SKIP'; printf '%s\n' "${1:+  $1}"; }
fail() { paint "$C_RED" 'FAILED'; printf '%s\n' "${1:+  $1}"; }
info() { paint "$C_CYAN" 'INFO'; printf '  %s\n' "$*"; }
warn() { paint "$C_YELLOW" 'WARN'; printf '  %s\n' "$*"; }
die() { paint "$C_RED" 'ERROR'; printf '  %s\n' "$*" >&2; exit 1; }

ensure_dirs() {
  mkdir -p "$RMCP_LOG_DIR" "$RMCP_SECRET_DIR" "$RMCP_BACKUP_DIR" "$RMCP_RUNTIME_DIR/bin"
  chmod 700 "$RMCP_SECRET_DIR" "$RMCP_BACKUP_DIR" 2>/dev/null || true
}

load_env() {
  [[ -f "$RMCP_ENV" ]] || return 1
  set -a
  # shellcheck disable=SC1090
  source "$RMCP_ENV"
  set +a
}

get_env_value() {
  local key="$1"
  [[ -f "$RMCP_ENV" ]] || return 1
  awk -F= -v k="$key" '$1==k {sub(/^[^=]*=/, ""); print; exit}' "$RMCP_ENV"
}

set_env_value() {
  local key="$1" value="$2" tmp
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || die "Refusing multiline value for $key"
  tmp="$(mktemp "$RMCP_ROOT/.env.tmp.XXXXXX")"
  if [[ -f "$RMCP_ENV" ]]; then
    awk -v k="$key" -v v="$value" '
      BEGIN { done=0 }
      $0 ~ "^" k "=" { print k "=" v; done=1; next }
      { print }
      END { if (!done) print k "=" v }
    ' "$RMCP_ENV" > "$tmp"
  else
    printf '%s=%s\n' "$key" "$value" > "$tmp"
  fi
  chmod 600 "$tmp"
  mv "$tmp" "$RMCP_ENV"
}

# Canonical RHMCP_* configuration with DSW_MCP_* migration compatibility.
# Existing legacy DSWD values win when both exist, matching the Python adapter.
mcp_env_value() {
  local suffix="$1" legacy generic
  legacy="$(get_env_value "DSW_MCP_${suffix}" 2>/dev/null || true)"
  if [[ -n "$legacy" ]]; then
    printf '%s\n' "$legacy"
    return 0
  fi
  generic="$(get_env_value "RHMCP_${suffix}" 2>/dev/null || true)"
  [[ -n "$generic" ]] || return 1
  printf '%s\n' "$generic"
}

set_mcp_env_value() {
  local suffix="$1" value="$2"
  if [[ -n "$(get_env_value "DSW_MCP_${suffix}" 2>/dev/null || true)" && -z "$(get_env_value "RHMCP_${suffix}" 2>/dev/null || true)" ]]; then
    set_env_value "DSW_MCP_${suffix}" "$value"
  else
    set_env_value "RHMCP_${suffix}" "$value"
  fi
}

backup_file() {
  local file="$1" label="$2" stamp dest
  [[ -f "$file" ]] || return 1
  ensure_dirs
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  dest="$RMCP_BACKUP_DIR/${stamp}-${label}"
  cp -p "$file" "$dest"
  chmod 600 "$dest" 2>/dev/null || true
  printf '%s\n' "$dest"
}

restore_file() {
  local backup="$1" dest="$2"
  [[ -f "$backup" ]] || die "Backup not found: $backup"
  cp -p "$backup" "$dest"
  chmod 600 "$dest" 2>/dev/null || true
}

valid_hostname() {
  local host="$1"
  [[ "$host" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]]
}

pid_alive() {
  local file="$1"
  [[ -f "$file" ]] &&
    [[ "$(cat "$file" 2>/dev/null || true)" =~ ^[0-9]+$ ]] &&
    kill -0 "$(cat "$file")" 2>/dev/null
}

cloudflared_bin() {
  if [[ -x "$RMCP_RUNTIME_DIR/bin/cloudflared" ]]; then
    printf '%s\n' "$RMCP_RUNTIME_DIR/bin/cloudflared"
  elif command -v cloudflared >/dev/null 2>&1; then
    command -v cloudflared
  else
    return 1
  fi
}

print_next_cft_command() {
  local host="${1:-YOUR_MCP_DOMAIN}" port
  port="$(mcp_env_value PORT 2>/dev/null || printf '8765')"
  cat <<EOF

NEXT: Configure a remotely-managed Cloudflare Tunnel

1. In Cloudflare: Networking -> Tunnels -> create/select a Tunnel.
2. Add a Published application / Public Hostname.
3. Set the service/origin to:

   http://127.0.0.1:${port}

4. Copy the Tunnel token.
5. Replace BOTH placeholders below, then run:

CFD_HOST='$host' CFD_TOKEN='PASTE_TUNNEL_TOKEN_HERE' bash scripts/setup-cft.sh

The MCP Path Key is generated automatically. Treat the full MCP URL as a credential.
EOF
}

print_final_connection() {
  load_env || die "Missing .env"
  local key host port
  key="$(mcp_env_value PATH_KEY 2>/dev/null || true)"
  host="$(mcp_env_value PUBLIC_HOST 2>/dev/null || true)"
  port="$(mcp_env_value PORT 2>/dev/null || printf '8765')"
  [[ -n "$key" && -n "$host" ]] || die "Missing MCP host/key"
  header 'Remote Host MCP 0.1.0-alpha.1 - DEPLOYMENT READY'
  printf 'Local MCP        : http://127.0.0.1:%s\n' "$port"
  printf 'Public Hostname  : %s\n' "$host"
  printf 'MCP Path Key     : %s\n' "$key"
  if [[ "$host" == "mcp.invalid" ]]; then
    printf 'Full MCP URL     : NOT AVAILABLE (configure public hostname first)\n'
  else
    printf 'Full MCP URL     : https://%s/mcp/%s\n' "$host" "$key"
  fi
  printf 'Authentication   : capability URL (OAuth optional)\n'
  subhr
  warn 'The full MCP URL contains a credential. Do not commit or publish it.'
  hr
}
