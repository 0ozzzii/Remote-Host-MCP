#!/usr/bin/env bash
set -euo pipefail

C_RESET='\033[0m'; C_GREEN='\033[32m'; C_YELLOW='\033[33m'; C_RED='\033[31m'; C_CYAN='\033[36m'
use_color() { [[ -t 1 && -z "${NO_COLOR:-}" ]]; }
paint() { local c="$1"; shift; if use_color; then printf '%b%s%b' "$c" "$*" "$C_RESET"; else printf '%s' "$*"; fi; }
hr() { printf '%s\n' '============================================================'; }
subhr() { printf '%s\n' '------------------------------------------------------------'; }
header() { hr; printf ' %s\n' "$1"; hr; }
ok() { paint "$C_GREEN" '[✓]'; printf ' %s\n' "$*"; }
warn() { paint "$C_YELLOW" '[!]'; printf ' %s\n' "$*"; }
fail() { paint "$C_RED" '[✗]'; printf ' %s\n' "$*" >&2; }
info() { paint "$C_CYAN" '[→]'; printf ' %s\n' "$*"; }
die() { fail "$*"; exit 1; }
command_exists() { command -v "$1" >/dev/null 2>&1; }

confirm() {
  local prompt="${1:-$(t confirm)}" answer
  read -r -p "$prompt [y/N]: " answer || true
  [[ "$answer" =~ ^[Yy]$ ]]
}

valid_hostname() {
  local host="$1"
  [[ "$host" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]]
}

require_python() {
  command_exists python3 || die 'python3 is required / 需要 python3'
  python3 - <<'PY' >/dev/null
import sys
assert sys.version_info >= (3, 10), sys.version
PY
}

sha256_file() {
  if command_exists sha256sum; then sha256sum "$1" | awk '{print $1}';
  else python3 - "$1" <<'PY'
import hashlib, pathlib, sys
print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
  fi
}
