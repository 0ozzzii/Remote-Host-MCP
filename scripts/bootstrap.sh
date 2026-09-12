#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export RMCP_ROOT="$ROOT"
source "$ROOT/scripts/lib.sh"
cd "$ROOT"
ensure_dirs
chmod +x rmcp scripts/*.sh install.sh installer/install.sh 2>/dev/null || true

header "Remote Host MCP $(project_version) - Legacy/source local bootstrap"

step '1/9' 'Checking Python 3'
if command -v python3 >/dev/null 2>&1; then ok "$(python3 --version 2>&1)"; else fail; die 'python3 is required.'; fi

step '2/9' 'Creating virtual environment'
if [[ -x .venv/bin/python ]]; then skip 'existing .venv'; else python3 -m venv .venv && ok || { fail; exit 1; }; fi

step '3/9' 'Installing/updating package'
if .venv/bin/python -m pip install -q --upgrade pip && .venv/bin/pip install -q -e '.[dev]'; then ok; else fail; exit 1; fi

step '4/9' 'Preparing configuration'
if [[ ! -f .env ]]; then cp .env.example .env; chmod 600 .env; ok 'created .env'; else skip 'existing .env kept'; fi

step '5/9' 'Generating MCP Path Key'
current_key="$(mcp_env_value PATH_KEY 2>/dev/null || true)"
if [[ "$current_key" =~ ^[A-Za-z0-9_-]{32,128}$ && "$current_key" != REPLACE_* ]]; then skip 'existing key kept'; else
  new_key="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"
  set_mcp_env_value PATH_KEY "$new_key"; ok 'generated and saved (not printed)'
fi

current_host="$(mcp_env_value PUBLIC_HOST 2>/dev/null || true)"
if ! valid_hostname "$current_host"; then set_mcp_env_value PUBLIC_HOST 'mcp.invalid'; current_host='mcp.invalid'; fi

step '6/9' 'Running test suite'
if .venv/bin/pytest -q; then ok; else fail 'tests failed; server was not restarted'; exit 1; fi

step '7/9' 'Starting local MCP server'
if bash scripts/manage.sh restart >/dev/null; then ok; else fail; bash scripts/manage.sh logs 80 || true; exit 1; fi

step '8/9' 'Checking localhost health'
port="$(mcp_env_value PORT 2>/dev/null || printf '8765')"
if curl -fsS --max-time 5 "http://127.0.0.1:${port}/health" >/dev/null; then ok; else fail; die 'Local MCP health failed. Run: bash scripts/manage.sh logs 120'; fi

printf '[9/9] Registering global rmcp command\n'
bash scripts/register-command.sh || warn 'Global command registration failed. Local MCP remains healthy.'
subhr
info "Remote Host MCP is healthy on 127.0.0.1:${port}."
info 'The MCP capability key is stored in .env.'
print_next_cft_command "$current_host"
subhr
printf 'Management menu from any directory: rmcp\n'
printf 'Show key / full URL later: rmcp -> connection information\n'
hr
