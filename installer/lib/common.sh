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

# Some container providers intentionally run the shell as a numeric UID that has
# no matching /etc/passwd entry. GNU `id -un` exits non-zero in that situation,
# which would abort the Installer under `set -e`. Preserve normal `id` behavior
# for every other invocation, but make the no-argument current-user lookup fall
# back to the stable numeric UID so prefix/portable installs can still proceed.
id() {
  if [[ "$#" -eq 1 && "$1" == '-un' ]]; then
    local current_name
    current_name="$(command id -un 2>/dev/null || true)"
    if [[ -n "$current_name" ]]; then
      printf '%s\n' "$current_name"
    else
      command id -u
    fi
    return 0
  fi
  command id "$@"
}

# Installer/runtime dependencies require Python >= 3.10. Prefer a compatible
# host interpreter when one exists. On older-Python hosts, the private-Python
# module provides a pinned, product-owned fallback without touching /usr/bin.
RHMCP_PYTHON_BIN="${RHMCP_PYTHON_BIN:-}"

_rhmcp_external_command() {
  local name="$1"
  if [[ "$name" == */* ]]; then
    [[ -x "$name" ]] && printf '%s\n' "$name"
  else
    type -P "$name" 2>/dev/null || true
  fi
}

_rhmcp_python_supported() {
  local candidate="$1"
  command "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

select_python_interpreter() {
  local requested="${RHMCP_PYTHON_BIN:-}" candidate resolved seen='|'
  local -a candidates=()

  if [[ -n "$requested" ]]; then
    resolved="$(_rhmcp_external_command "$requested")"
    if [[ -n "$resolved" ]] && _rhmcp_python_supported "$resolved"; then
      RHMCP_PYTHON_BIN="$resolved"
      export RHMCP_PYTHON_BIN
      return 0
    fi
    fail "Configured RHMCP_PYTHON_BIN is unavailable or older than Python 3.10: $requested"
    return 1
  fi

  if [[ -n "${RHMCP_PYTHON_CANDIDATES:-}" ]]; then
    IFS=':' read -r -a candidates <<< "$RHMCP_PYTHON_CANDIDATES"
  else
    candidates=(python3 /usr/bin/python3 /usr/local/bin/python3 python3.14 python3.13 python3.12 python3.11 python3.10)
  fi

  for candidate in "${candidates[@]}"; do
    [[ -n "$candidate" ]] || continue
    resolved="$(_rhmcp_external_command "$candidate")"
    [[ -n "$resolved" ]] || continue
    [[ "$seen" != *"|$resolved|"* ]] || continue
    seen+="$resolved|"
    if _rhmcp_python_supported "$resolved"; then
      RHMCP_PYTHON_BIN="$resolved"
      export RHMCP_PYTHON_BIN
      return 0
    fi
  done
  return 1
}

python3() {
  local bin="${RHMCP_PYTHON_BIN:-}"
  if declare -F private_python_maybe_publish >/dev/null 2>&1; then
    private_python_maybe_publish || return 1
    bin="${RHMCP_PYTHON_BIN:-$bin}"
  fi
  if [[ -z "$bin" ]]; then bin="$(type -P python3 2>/dev/null || true)"; fi
  [[ -n "$bin" ]] || { printf 'python3: command not found\n' >&2; return 127; }
  command "$bin" "$@"
}

# Non-interactive mode (--non-interactive / RHMCP_NON_INTERACTIVE=1) turns every
# missing required value into a hard failure instead of a prompt. Defaults are
# only ever applied to prompts that document a bracketed default.
non_interactive() {
  case "${RHMCP_NON_INTERACTIVE:-0}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

ni_missing() {
  local name="$1" what="${2:-}"
  fail "Non-interactive install is missing a required value: ${name}${what:+ (${what})}"
  fail "非交互安装缺少必需变量：${name}${what:+（${what}）}"
  fail "Re-run with ${name}=<value>, or run the installer interactively without --non-interactive."
  fail "请设置 ${name}=<值> 后重试，或不带 --non-interactive 交互式运行安装器。"
  exit 1
}

# Operator prompts can be satisfied from the environment. When the matching
# RHMCP_* variable is already set the prompt is skipped and the supplied value is
# used verbatim; when it is unset the historical interactive behaviour is kept
# exactly as-is. This is what lets a remote panel drive the installer.
ni_prompt() {
  local target="$1" name="$2" prompt="$3" allowed="${4:-}" value token ok
  value="${!name:-}"
  if [[ -n "$value" ]]; then
    if [[ -n "$allowed" ]]; then
      ok=0
      for token in $allowed; do
        if [[ "$value" == "$token" ]]; then ok=1; break; fi
      done
      if (( ok == 0 )); then
        fail "Invalid ${name}: '${value}' / ${name} 取值无效：'${value}'"
        fail "Allowed / 允许值: ${allowed// /, }"
        exit 1
      fi
    fi
    printf -v "$target" '%s' "$value"
    return 0
  fi
  if non_interactive; then ni_missing "$name" "$prompt"; fi
  read -r -p "$prompt" value || true
  printf -v "$target" '%s' "$value"
}

# Same as ni_prompt for prompts that accept an empty answer (bracketed default).
ni_prompt_default() {
  local target="$1" name="$2" prompt="$3" fallback="${4:-}" value
  value="${!name:-}"
  if [[ -z "$value" ]]; then
    if non_interactive; then
      value="$fallback"
      info "Non-interactive: ${name} unset, using default '${fallback}' / ${name} 未设置，使用默认值 '${fallback}'"
    else
      read -r -p "$prompt" value || true
      value="${value:-$fallback}"
    fi
  fi
  printf -v "$target" '%s' "$value"
}

confirm() {
  local prompt="${1:-$(t confirm)}" answer
  if [[ "${RHMCP_ASSUME_YES:-0}" == 1 ]]; then return 0; fi
  if non_interactive; then
    ni_missing RHMCP_ASSUME_YES "confirmation required: ${prompt}"
  fi
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
  local default_bin default_version='not found' requested="${RHMCP_PYTHON_BIN:-}"
  default_bin="$(type -P python3 2>/dev/null || true)"
  if [[ -n "$default_bin" ]]; then default_version="$(command "$default_bin" --version 2>&1 || true)"; fi

  if select_python_interpreter; then return 0; fi

  # An explicit override is an operator assertion. If it is wrong, fail closed
  # rather than silently downloading a different interpreter.
  if [[ -n "$requested" ]]; then
    fail 'Configured Python did not satisfy the Python >= 3.10 contract. / 指定的 Python 不满足 >= 3.10 要求。'
    return 1
  fi

  if declare -F private_python_select_or_bootstrap >/dev/null 2>&1 && private_python_select_or_bootstrap; then
    return 0
  fi

  fail 'System environment check failed: Remote Host MCP requires Python >= 3.10. / 系统环境检测失败：Remote Host MCP 需要 Python >= 3.10。'
  fail "Detected default python3: ${default_version:-unknown} / 当前默认 python3：${default_version:-unknown}"
  printf '%s\n' 'NEXT / 下一步: rerun and choose the product-owned private Python bootstrap, or set RHMCP_PYTHON_BIN=/absolute/path/to/python3.x.' >&2
  return 1
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

# Keep the bootstrap implementation isolated from generic shell helpers. It is
# sourced here so require_python() can use it while later ownership/state
# helpers remain available when the module actually publishes a runtime.
_RHMCP_COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$_RHMCP_COMMON_DIR/private_python.sh" ]]; then
  # shellcheck disable=SC1091
  source "$_RHMCP_COMMON_DIR/private_python.sh"
fi
unset _RHMCP_COMMON_DIR
