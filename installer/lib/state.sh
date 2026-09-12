#!/usr/bin/env bash
set -euo pipefail

state_escape() {
  printf '%q' "$1"
}

write_install_state() {
  local tmp="$INSTALL_STATE.tmp.$$"
  umask 077
  {
    printf 'RHMCP_INSTALL_VERSION=%s\n' "$(state_escape "$RMCP_VERSION")"
    printf 'RHMCP_INSTALL_MODE=%s\n' "$(state_escape "$INSTALL_MODE")"
    printf 'RHMCP_CODE_BASE=%s\n' "$(state_escape "$CODE_BASE")"
    printf 'RHMCP_CONFIG_DIR=%s\n' "$(state_escape "$CONFIG_DIR")"
    printf 'RHMCP_STATE_DIR_PERSIST=%s\n' "$(state_escape "$STATE_DIR")"
    printf 'RHMCP_LOG_DIR=%s\n' "$(state_escape "$LOG_DIR")"
    printf 'RHMCP_SECRET_DIR_PERSIST=%s\n' "$(state_escape "$SECRET_DIR")"
    printf 'RHMCP_RUNTIME_DIR_PERSIST=%s\n' "$(state_escape "$RUNTIME_DIR")"
    printf 'RHMCP_LANGUAGE=%s\n' "$(state_escape "$RMCP_LANGUAGE")"
    printf 'RHMCP_AUTHORITY=%s\n' "$(state_escape "$AUTHORITY")"
    printf 'RHMCP_INGRESS=%s\n' "$(state_escape "$INGRESS")"
    printf 'RHMCP_LOCAL_PORT=%s\n' "$(state_escape "$LOCAL_PORT")"
    printf 'RHMCP_PUBLIC_HOST_STATE=%s\n' "$(state_escape "$PUBLIC_HOST")"
    printf 'RHMCP_SERVICE_BACKEND=%s\n' "$(state_escape "$SERVICE_BACKEND")"
  } > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$INSTALL_STATE"
}

load_install_state() {
  local file="$1"
  [[ -f "$file" ]] || return 1
  # State is root/user-owned installer metadata, never arbitrary remote input.
  # shellcheck disable=SC1090
  source "$file"
}
