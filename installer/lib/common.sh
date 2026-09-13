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

valid_ipv4() {
  local ip="$1"
  python3 - "$ip" <<'PY' >/dev/null 2>&1
import ipaddress, sys
addr = ipaddress.ip_address(sys.argv[1])
assert addr.version == 4 and not addr.is_unspecified and not addr.is_multicast
PY
}

require_python() {
  command_exists python3 || die 'python3 is required / 需要 python3'
  python3 - <<'PY' >/dev/null
import sys
assert sys.version_info >= (3, 10), sys.version
PY
}

python_venv_preflight() {
  local tmp err
  tmp="$(mktemp -d)"
  err="$tmp/venv.err"
  if ! python3 -m venv "$tmp/venv" >"$tmp/venv.out" 2>"$err"; then
    fail 'Python venv/ensurepip preflight failed before installation writes. / Python venv/ensurepip 预检失败，尚未写入安装资源。'
    sed -n '1,12p' "$err" >&2 || true
    rm -rf "$tmp"
    return 1
  fi
  if ! "$tmp/venv/bin/python" -m ensurepip --version >/dev/null 2>"$err"; then
    fail 'Python ensurepip is unavailable. Install the matching pythonX.Y-venv package first. / ensurepip 不可用，请先安装对应 pythonX.Y-venv。'
    sed -n '1,12p' "$err" >&2 || true
    rm -rf "$tmp"
    return 1
  fi
  "$tmp/venv/bin/python" -m pip --version >/dev/null 2>&1 || { rm -rf "$tmp"; return 1; }
  rm -rf "$tmp"
}

systemd_operational() {
  command_exists systemctl && [[ -d /run/systemd/system ]] && systemctl show --property=Version --value >/dev/null 2>&1
}

require_disk_space_mb() {
  local path="$1" min_mb="${2:-512}" probe avail_kb
  probe="$path"
  while [[ ! -e "$probe" && "$probe" != / ]]; do probe="$(dirname "$probe")"; done
  avail_kb="$(df -Pk "$probe" | awk 'NR==2 {print $4}')"
  [[ "$avail_kb" =~ ^[0-9]+$ ]] || return 1
  (( avail_kb >= min_mb * 1024 ))
}

safe_tail_file() {
  local file="$1" lines="${2:-40}"
  [[ -f "$file" ]] || return 0
  tail -n "$lines" "$file" 2>/dev/null | sed -E \
    -e 's#(https?://[^/[:space:]]+/mcp/)[A-Za-z0-9._~+/=-]+#\1<redacted>#g' \
    -e 's#([Tt]oken|[Kk]ey|[Ss]ecret)([= :]+)[^[:space:]]+#\1\2<redacted>#g'
}

sha256_file() {
  if command_exists sha256sum; then sha256sum "$1" | awk '{print $1}';
  else python3 - "$1" <<'PY'
import hashlib, pathlib, sys
print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
  fi
}
