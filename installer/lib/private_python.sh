#!/usr/bin/env bash
set -euo pipefail

# Product-owned CPython fallback for hosts whose system Python is older than the
# Remote Host MCP runtime contract. The pin is release truth: do not replace it
# with a moving latest URL.
RHMCP_PRIVATE_PYTHON_VERSION='3.11.16'
RHMCP_PRIVATE_PYTHON_RELEASE='20260901'
RHMCP_PRIVATE_PYTHON_GNU_ARTIFACT='cpython-3.11.16+20260901-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz'
RHMCP_PRIVATE_PYTHON_GNU_SHA256='64427febea27864d136db46c8efe968eb6fa5ca2813ce1dca4bb95aec31cb2e4'
RHMCP_PRIVATE_PYTHON_MUSL_ARTIFACT='cpython-3.11.16+20260901-x86_64-unknown-linux-musl-install_only_stripped.tar.gz'
RHMCP_PRIVATE_PYTHON_MUSL_SHA256='34418632930361c52693e6d4cfd91d3a124b5139a1bed31eb7e77d4f0a955053'
RHMCP_PRIVATE_PYTHON_ARTIFACT="$RHMCP_PRIVATE_PYTHON_GNU_ARTIFACT"
RHMCP_PRIVATE_PYTHON_SHA256="$RHMCP_PRIVATE_PYTHON_GNU_SHA256"
RHMCP_PRIVATE_PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${RHMCP_PRIVATE_PYTHON_RELEASE}/${RHMCP_PRIVATE_PYTHON_ARTIFACT}"
RHMCP_PRIVATE_PYTHON_STAGING_ROOT="${RHMCP_PRIVATE_PYTHON_STAGING_ROOT:-}"
RHMCP_PRIVATE_PYTHON_ARCHIVE="${RHMCP_PRIVATE_PYTHON_ARCHIVE:-}"
RHMCP_PRIVATE_PYTHON_ACTIVE=false

_private_python_effective_url() {
  if [[ "${RHMCP_TESTING:-0}" == 1 && -n "${RHMCP_PRIVATE_PYTHON_TEST_URL:-}" ]]; then
    printf '%s\n' "$RHMCP_PRIVATE_PYTHON_TEST_URL"
  else
    printf '%s\n' "$RHMCP_PRIVATE_PYTHON_URL"
  fi
}

_private_python_effective_sha256() {
  if [[ "${RHMCP_TESTING:-0}" == 1 && -n "${RHMCP_PRIVATE_PYTHON_TEST_SHA256:-}" ]]; then
    printf '%s\n' "$RHMCP_PRIVATE_PYTHON_TEST_SHA256"
  else
    printf '%s\n' "$RHMCP_PRIVATE_PYTHON_SHA256"
  fi
}

_private_python_final_dir() {
  local runtime="${RUNTIME_DIR:-${RHMCP_RUNTIME_DIR_PERSIST:-}}"
  [[ -n "$runtime" ]] || return 1
  printf '%s/private-python-%s\n' "${runtime%/}" "$RHMCP_PRIVATE_PYTHON_VERSION"
}

_private_python_bin_for_root() {
  printf '%s/python/bin/python3\n' "${1%/}"
}

_private_python_marker_for_root() {
  printf '%s/.rhmcp-private-python.env\n' "${1%/}"
}

_private_python_musl_loader_present() {
  if [[ "${RHMCP_TESTING:-0}" == 1 && -n "${RHMCP_PRIVATE_PYTHON_TEST_MUSL_LOADER_PRESENT:-}" ]]; then
    [[ "$RHMCP_PRIVATE_PYTHON_TEST_MUSL_LOADER_PRESENT" == 1 ]]
    return
  fi
  compgen -G '/lib/ld-musl-*.so.1' >/dev/null 2>&1 || compgen -G '/usr/lib/ld-musl-*.so.1' >/dev/null 2>&1
}

_private_python_detect_libc_family() {
  local libc=''
  if [[ "${RHMCP_TESTING:-0}" == 1 && -n "${RHMCP_PRIVATE_PYTHON_TEST_LIBC:-}" ]]; then
    printf '%s\n' "$RHMCP_PRIVATE_PYTHON_TEST_LIBC"
    return 0
  fi

  if command -v getconf >/dev/null 2>&1; then
    libc="$(getconf GNU_LIBC_VERSION 2>/dev/null || true)"
    if printf '%s' "$libc" | grep -qi musl; then
      printf '%s\n' musl
      return 0
    fi
    if [[ "$libc" == *glibc* || "$libc" == *GLIBC* || "$libc" == *'GNU libc'* || "$libc" == *'GNU C Library'* ]]; then
      printf '%s\n' gnu
      return 0
    fi
  fi

  if command -v ldd >/dev/null 2>&1; then
    libc="$(ldd --version 2>&1 | head -n 1 || true)"
    if printf '%s' "$libc" | grep -qi musl; then
      printf '%s\n' musl
      return 0
    fi
    if [[ "$libc" == *glibc* || "$libc" == *GLIBC* || "$libc" == *'GNU libc'* || "$libc" == *'GNU C Library'* ]]; then
      printf '%s\n' gnu
      return 0
    fi
  fi

  if _private_python_musl_loader_present; then
    printf '%s\n' musl
  else
    printf '%s\n' unknown
  fi
}

_private_python_select_platform_artifact() {
  local os arch libc_family
  os="$(uname -s 2>/dev/null || true)"
  arch="$(uname -m 2>/dev/null || true)"
  [[ "$os" == Linux ]] || { fail "Private Python bootstrap currently supports Linux only (detected: ${os:-unknown})."; return 1; }
  case "$arch" in x86_64|amd64) ;; *) fail "Private Python bootstrap currently supports x86_64 only (detected: ${arch:-unknown})."; return 1 ;; esac
  libc_family="$(_private_python_detect_libc_family)"
  case "$libc_family" in
    gnu)
      RHMCP_PRIVATE_PYTHON_ARTIFACT="$RHMCP_PRIVATE_PYTHON_GNU_ARTIFACT"
      RHMCP_PRIVATE_PYTHON_SHA256="$RHMCP_PRIVATE_PYTHON_GNU_SHA256"
      ;;
    musl)
      RHMCP_PRIVATE_PYTHON_ARTIFACT="$RHMCP_PRIVATE_PYTHON_MUSL_ARTIFACT"
      RHMCP_PRIVATE_PYTHON_SHA256="$RHMCP_PRIVATE_PYTHON_MUSL_SHA256"
      ;;
    *)
      fail 'Could not positively identify glibc or musl; refusing private Python artifact fallback.'
      return 1
      ;;
  esac
  RHMCP_PRIVATE_PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${RHMCP_PRIVATE_PYTHON_RELEASE}/${RHMCP_PRIVATE_PYTHON_ARTIFACT}"
  export RHMCP_PRIVATE_PYTHON_ARTIFACT RHMCP_PRIVATE_PYTHON_SHA256 RHMCP_PRIVATE_PYTHON_URL
}

_private_python_platform_supported() {
  _private_python_select_platform_artifact
}

_private_python_marker_matches() {
  local root="$1" marker version release artifact sha
  marker="$(_private_python_marker_for_root "$root")"
  [[ -f "$marker" ]] || return 1
  version="$(sed -n 's/^RHMCP_PRIVATE_PYTHON_VERSION=//p' "$marker" | head -n1)"
  release="$(sed -n 's/^RHMCP_PRIVATE_PYTHON_RELEASE=//p' "$marker" | head -n1)"
  artifact="$(sed -n 's/^RHMCP_PRIVATE_PYTHON_ARTIFACT=//p' "$marker" | head -n1)"
  sha="$(sed -n 's/^RHMCP_PRIVATE_PYTHON_SHA256=//p' "$marker" | head -n1)"
  [[ "$version" == "$RHMCP_PRIVATE_PYTHON_VERSION" && "$release" == "$RHMCP_PRIVATE_PYTHON_RELEASE" ]] || return 1
  [[ "$artifact" == "$RHMCP_PRIVATE_PYTHON_GNU_ARTIFACT" && "$sha" == "$RHMCP_PRIVATE_PYTHON_GNU_SHA256" ]] && return 0
  [[ "$artifact" == "$RHMCP_PRIVATE_PYTHON_MUSL_ARTIFACT" && "$sha" == "$RHMCP_PRIVATE_PYTHON_MUSL_SHA256" ]]
}

_private_python_runtime_healthy() {
  local root="$1" bin reported
  _private_python_marker_matches "$root" || return 1
  bin="$(_private_python_bin_for_root "$root")"
  [[ -x "$bin" ]] || return 1
  reported="$(command "$bin" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || true)"
  [[ "$reported" == "$RHMCP_PRIVATE_PYTHON_VERSION" ]] || return 1
  command "$bin" -c 'import ensurepip, venv' >/dev/null 2>&1 || return 1
}

_private_python_write_marker() {
  local root="$1" marker tmp
  marker="$(_private_python_marker_for_root "$root")"
  tmp="${marker}.tmp.$$"
  umask 077
  {
    printf 'RHMCP_PRIVATE_PYTHON_VERSION=%s\n' "$RHMCP_PRIVATE_PYTHON_VERSION"
    printf 'RHMCP_PRIVATE_PYTHON_RELEASE=%s\n' "$RHMCP_PRIVATE_PYTHON_RELEASE"
    printf 'RHMCP_PRIVATE_PYTHON_ARTIFACT=%s\n' "$RHMCP_PRIVATE_PYTHON_ARTIFACT"
    printf 'RHMCP_PRIVATE_PYTHON_SHA256=%s\n' "$RHMCP_PRIVATE_PYTHON_SHA256"
    printf 'RHMCP_PRIVATE_PYTHON_SOURCE=%s\n' 'astral-sh/python-build-standalone'
  } > "$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$marker"
}

_private_python_archive_safe() {
  local archive="$1"
  tar -tzf "$archive" | awk '
    BEGIN { bad=0 }
    /^\// { bad=1 }
    /(^|\/)\.\.($|\/)/ { bad=1 }
    END { exit bad }
  '
}

_private_python_download_archive() {
  local workspace archive part expected actual url
  workspace="$(mktemp -d "${TMPDIR:-/tmp}/rhmcp-private-python.XXXXXX")"
  archive="$workspace/$RHMCP_PRIVATE_PYTHON_ARTIFACT"
  part="${archive}.part"
  expected="$(_private_python_effective_sha256)"
  url="$(_private_python_effective_url)"

  if [[ "${RHMCP_TESTING:-0}" == 1 && -n "${RHMCP_PRIVATE_PYTHON_TEST_ARCHIVE:-}" ]]; then
    cp -- "$RHMCP_PRIVATE_PYTHON_TEST_ARCHIVE" "$part" || { rm -rf -- "$workspace"; return 1; }
  else
    curl -fL --retry 2 --connect-timeout 10 --max-time "${RHMCP_PRIVATE_PYTHON_DOWNLOAD_TIMEOUT_S:-180}" "$url" -o "$part" || {
      rm -rf -- "$workspace"
      fail 'Private Python download failed; no runtime was published.'
      return 1
    }
  fi
  mv -f "$part" "$archive"
  actual="$(sha256_file "$archive")"
  if [[ "$actual" != "$expected" ]]; then
    rm -rf -- "$workspace"
    fail "Private Python checksum mismatch; expected $expected, got ${actual:-unavailable}."
    return 1
  fi
  _private_python_archive_safe "$archive" || { rm -rf -- "$workspace"; fail 'Private Python archive contains unsafe paths.'; return 1; }
  RHMCP_PRIVATE_PYTHON_STAGING_ROOT="$workspace"
  RHMCP_PRIVATE_PYTHON_ARCHIVE="$archive"
  export RHMCP_PRIVATE_PYTHON_STAGING_ROOT RHMCP_PRIVATE_PYTHON_ARCHIVE
}

_private_python_bootstrap_for_preflight() {
  local extracted bin
  _private_python_platform_supported || return 1
  _private_python_download_archive || return 1
  extracted="$RHMCP_PRIVATE_PYTHON_STAGING_ROOT/preflight"
  mkdir -p "$extracted"
  tar -xzf "$RHMCP_PRIVATE_PYTHON_ARCHIVE" -C "$extracted" || {
    rm -rf -- "$RHMCP_PRIVATE_PYTHON_STAGING_ROOT"
    RHMCP_PRIVATE_PYTHON_STAGING_ROOT=''; RHMCP_PRIVATE_PYTHON_ARCHIVE=''
    fail 'Private Python extraction failed; no runtime was published.'
    return 1
  }
  _private_python_write_marker "$extracted"
  _private_python_runtime_healthy "$extracted" || {
    rm -rf -- "$RHMCP_PRIVATE_PYTHON_STAGING_ROOT"
    RHMCP_PRIVATE_PYTHON_STAGING_ROOT=''; RHMCP_PRIVATE_PYTHON_ARCHIVE=''
    fail 'Downloaded private Python failed exact version/venv validation.'
    return 1
  }
  bin="$(_private_python_bin_for_root "$extracted")"
  RHMCP_PYTHON_BIN="$bin"
  RHMCP_PRIVATE_PYTHON_ACTIVE=true
  : "${RHMCP_BASE_MIN_FREE_MB:=512}"
  : "${RHMCP_MIN_FREE_MB:=768}"
  export RHMCP_PYTHON_BIN RHMCP_PRIVATE_PYTHON_ACTIVE RHMCP_BASE_MIN_FREE_MB RHMCP_MIN_FREE_MB
}

_private_python_use_existing_if_healthy() {
  local final bin
  final="$(_private_python_final_dir 2>/dev/null || true)"
  [[ -n "$final" ]] || return 1
  _private_python_runtime_healthy "$final" || return 1
  bin="$(_private_python_bin_for_root "$final")"
  RHMCP_PYTHON_BIN="$bin"
  RHMCP_PRIVATE_PYTHON_ACTIVE=true
  export RHMCP_PYTHON_BIN RHMCP_PRIVATE_PYTHON_ACTIVE
}

_private_python_text() {
  local key="$1" fallback="$2" value=''
  if declare -F t >/dev/null 2>&1; then
    value="$(t "$key")"
    if [[ -n "$value" && "$value" != "$key" ]]; then
      printf '%s' "$value"
      return 0
    fi
  fi
  printf '%s' "$fallback"
}

private_python_select_or_bootstrap() {
  local choice candidate
  if _private_python_use_existing_if_healthy; then
    printf -v candidate "$(_private_python_text private_python_using 'Product-owned Python %s')" "$RHMCP_PRIVATE_PYTHON_VERSION"
    ok "$candidate"
    return 0
  fi
  printf '\n%s\n' "$(_private_python_text python_missing 'No usable Python >= 3.10 was found.')"
  printf '  1. '; printf "$(_private_python_text private_python_install 'Install Remote Host MCP private Python %s [recommended]')" "$RHMCP_PRIVATE_PYTHON_VERSION"; printf '\n'
  printf '  2. %s\n' "$(_private_python_text private_python_existing 'Specify an existing Python path')"
  printf '  3. %s\n' "$(_private_python_text private_python_exit 'Exit')"
  choice="${RHMCP_PRIVATE_PYTHON_CHOICE:-}"
  if [[ -z "$choice" ]]; then
    if non_interactive; then
      fail "$(_private_python_text private_python_noninteractive 'Non-interactive install requires RHMCP_PRIVATE_PYTHON_CHOICE=1 (bootstrap), 2 (existing path), or 3 (exit).')"
      return 1
    fi
    if [[ -t 0 ]]; then
      read -r -p "$(_private_python_text private_python_select 'Select [1-3, default 1]: ')" choice || true
    else
      fail "$(_private_python_text private_python_noninteractive 'Non-interactive install requires RHMCP_PRIVATE_PYTHON_CHOICE=1 (bootstrap), 2 (existing path), or 3 (exit).')"
      return 1
    fi
  fi
  case "${choice:-1}" in
    1)
      _private_python_bootstrap_for_preflight
      ;;
    2)
      candidate="${RHMCP_PRIVATE_PYTHON_EXISTING_BIN:-}"
      if [[ -z "$candidate" ]]; then
        if non_interactive; then
          fail "$(_private_python_text private_python_noninteractive 'Non-interactive install requires RHMCP_PRIVATE_PYTHON_EXISTING_BIN=<absolute python path>.')"
          return 1
        fi
        read -r -p "$(_private_python_text private_python_existing_path 'Absolute Python path: ')" candidate
      fi
      [[ "$candidate" = /* ]] || { fail "$(_private_python_text private_python_path_absolute 'Python path must be absolute.')"; return 1; }
      RHMCP_PYTHON_BIN="$candidate"; export RHMCP_PYTHON_BIN
      select_python_interpreter
      ;;
    3)
      return 1
      ;;
    *)
      fail "$(_private_python_text private_python_invalid 'Invalid private Python selection.')"
      return 1
      ;;
  esac
}

_private_python_publish_archive() {
  local archive="$1" final runtime stage old bin
  runtime="${RUNTIME_DIR:-${RHMCP_RUNTIME_DIR_PERSIST:-}}"
  [[ -n "$runtime" && -d "$runtime" ]] || return 2
  final="$(_private_python_final_dir)"
  if _private_python_runtime_healthy "$final"; then
    RHMCP_PYTHON_BIN="$(_private_python_bin_for_root "$final")"
    export RHMCP_PYTHON_BIN
    return 0
  fi
  if [[ -e "$final" ]] && ! _private_python_marker_matches "$final"; then
    fail "Refusing to replace unowned or mismatched private Python directory: $final"
    return 1
  fi
  find "$runtime" -maxdepth 1 -type d -name '.private-python-staging.*' -exec rm -rf -- {} + 2>/dev/null || true
  stage="$runtime/.private-python-staging.$$"
  mkdir -p "$stage"
  tar -xzf "$archive" -C "$stage" || { rm -rf -- "$stage"; fail 'Private Python final extraction failed.'; return 1; }
  _private_python_write_marker "$stage"
  _private_python_runtime_healthy "$stage" || { rm -rf -- "$stage"; fail 'Private Python staged runtime failed validation.'; return 1; }

  old=''
  if [[ -e "$final" ]]; then
    old="$runtime/.private-python-replaced.$$"
    mv -T -- "$final" "$old"
  fi
  if ! mv -T -- "$stage" "$final"; then
    [[ -n "$old" && -e "$old" ]] && mv -T -- "$old" "$final" || true
    rm -rf -- "$stage"
    return 1
  fi
  if ! _private_python_runtime_healthy "$final"; then
    rm -rf -- "$final"
    [[ -n "$old" && -e "$old" ]] && mv -T -- "$old" "$final" || true
    fail 'Private Python post-publish validation failed; previous owned runtime restored when available.'
    return 1
  fi
  [[ -n "$old" ]] && rm -rf -- "$old"
  bin="$(_private_python_bin_for_root "$final")"
  RHMCP_PYTHON_BIN="$bin"
  RHMCP_PRIVATE_PYTHON_ACTIVE=true
  export RHMCP_PYTHON_BIN RHMCP_PRIVATE_PYTHON_ACTIVE
  if declare -F record_resource >/dev/null 2>&1 && [[ -n "${OWNERSHIP_STATE:-}" ]]; then
    record_resource private_python "$final" created
  fi
}

private_python_maybe_publish() {
  [[ "${RHMCP_PRIVATE_PYTHON_ACTIVE:-false}" == true ]] || return 0
  [[ -n "${RUNTIME_DIR:-}" && -d "${RUNTIME_DIR:-}" ]] || return 0
  local final
  final="$(_private_python_final_dir)"
  if _private_python_runtime_healthy "$final"; then
    RHMCP_PYTHON_BIN="$(_private_python_bin_for_root "$final")"; export RHMCP_PYTHON_BIN
    return 0
  fi
  [[ -n "${RHMCP_PRIVATE_PYTHON_ARCHIVE:-}" && -f "$RHMCP_PRIVATE_PYTHON_ARCHIVE" ]] || {
    fail 'Private Python is active but its verified bootstrap archive is unavailable for publication.'
    return 1
  }
  _private_python_publish_archive "$RHMCP_PRIVATE_PYTHON_ARCHIVE" || return 1
  [[ -n "${RHMCP_PRIVATE_PYTHON_STAGING_ROOT:-}" ]] && rm -rf -- "$RHMCP_PRIVATE_PYTHON_STAGING_ROOT"
  RHMCP_PRIVATE_PYTHON_STAGING_ROOT=''; RHMCP_PRIVATE_PYTHON_ARCHIVE=''
  export RHMCP_PRIVATE_PYTHON_STAGING_ROOT RHMCP_PRIVATE_PYTHON_ARCHIVE
}

private_python_repair_for_layout() {
  local owned_path ownership
  owned_path="$(resource_value private_python PATH 2>/dev/null || true)"
  ownership="$(resource_value private_python OWNERSHIP 2>/dev/null || true)"
  [[ -n "$owned_path" ]] || return 0
  [[ "$ownership" == created ]] || { fail 'Private Python ownership metadata is not product-created; refusing repair.'; return 1; }
  if _private_python_runtime_healthy "$owned_path"; then
    RHMCP_PYTHON_BIN="$(_private_python_bin_for_root "$owned_path")"; export RHMCP_PYTHON_BIN
    return 0
  fi
  warn 'Owned private Python is corrupt or does not match the pinned version; rebuilding it.'
  _private_python_platform_supported || return 1
  _private_python_download_archive || return 1
  _private_python_publish_archive "$RHMCP_PRIVATE_PYTHON_ARCHIVE" || return 1
  rm -rf -- "$RHMCP_PRIVATE_PYTHON_STAGING_ROOT"
  RHMCP_PRIVATE_PYTHON_STAGING_ROOT=''; RHMCP_PRIVATE_PYTHON_ARCHIVE=''
}

private_python_diagnose() {
  local path ownership status='not-used'
  path="$(resource_value private_python PATH 2>/dev/null || true)"
  ownership="$(resource_value private_python OWNERSHIP 2>/dev/null || true)"
  if [[ -n "$path" ]]; then
    if _private_python_runtime_healthy "$path"; then status="healthy"; else status="invalid"; fi
    printf 'Private Python     : %s (%s, ownership=%s)\n' "$RHMCP_PRIVATE_PYTHON_VERSION" "$status" "${ownership:-unknown}"
    printf 'Private Python dir : %s\n' "$path"
  else
    printf 'Private Python     : not used / system interpreter\n'
  fi
}
