#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export RMCP_ROOT="$ROOT"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib.sh"
cd "$ROOT"
ensure_dirs

header 'Remote Host MCP 0.1.0-alpha.1 - Register global command'

path_has_dir() {
  local needle="$1" entry
  IFS=':' read -r -a entries <<< "${PATH:-}"
  for entry in "${entries[@]}"; do
    [[ "$entry" == "$needle" ]] && return 0
  done
  return 1
}

choose_bin_dir() {
  if [[ -n "${RMCP_BIN_DIR:-}" ]]; then
    printf '%s\n' "$RMCP_BIN_DIR"
    return
  fi

  if [[ -d /usr/local/bin && -w /usr/local/bin ]]; then
    printf '%s\n' /usr/local/bin
    return
  fi

  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    mkdir -p /usr/local/bin
    printf '%s\n' /usr/local/bin
    return
  fi

  if path_has_dir "$HOME/.local/bin"; then
    printf '%s\n' "$HOME/.local/bin"
    return
  fi

  if path_has_dir "$HOME/bin"; then
    printf '%s\n' "$HOME/bin"
    return
  fi

  printf '%s\n' "$HOME/.local/bin"
}

bin_dir="$(choose_bin_dir)"
mkdir -p "$bin_dir"
launcher="$bin_dir/rmcp"

step '1/3' 'Writing global rmcp launcher'
tmp="$(mktemp "$bin_dir/.rmcp.tmp.XXXXXX")"
{
  printf '%s\n' '#!/usr/bin/env bash'
  printf 'exec bash %q "$@"\n' "$ROOT/scripts/rmcp.sh"
} > "$tmp"
chmod 755 "$tmp"
mv "$tmp" "$launcher"
ok "$launcher"

step '2/3' 'Verifying launcher target'
resolved="$("$launcher" --root 2>/dev/null || true)"
if [[ "$resolved" == "$ROOT" ]]; then
  ok "$resolved"
else
  fail
  rm -f "$launcher"
  die 'Global launcher verification failed; launcher was removed.'
fi

step '3/3' 'Checking command visibility'
if path_has_dir "$bin_dir"; then
  ok 'rmcp is available from any directory in this shell'
  subhr
  printf 'Global command: rmcp\n'
  printf 'Project root  : %s\n' "$ROOT"
  printf 'Launcher      : %s\n' "$launcher"
else
  skip "$bin_dir is not currently in PATH"
  subhr
  warn 'The launcher is installed, but this shell cannot find rmcp by name yet.'
  printf 'Run this once in the current shell:\n\n'
  printf '  export PATH=%q:"$PATH"\n\n' "$bin_dir"
  printf 'For future logins, add this directory to your shell PATH.\n'
  printf 'You can always repair/re-register it with:\n\n'
  printf '  bash %q\n' "$ROOT/scripts/register-command.sh"
fi

hr
