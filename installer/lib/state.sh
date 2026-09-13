#!/usr/bin/env bash
set -euo pipefail

state_escape() { printf '%q' "$1"; }

resolve_build_provenance() {
  local git_ref git_sha
  git_ref=''; git_sha=''
  if git -C "$SOURCE_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git_sha="$(git -C "$SOURCE_ROOT" rev-parse HEAD)"
    git_ref="$(git -C "$SOURCE_ROOT" symbolic-ref -q --short HEAD 2>/dev/null || git -C "$SOURCE_ROOT" describe --tags --exact-match 2>/dev/null || printf '%s' "$git_sha")"
  fi
  REQUESTED_REF="${RHMCP_REQUESTED_REF:-$git_ref}"
  RESOLVED_COMMIT="${RHMCP_RESOLVED_COMMIT:-$git_sha}"
  [[ "$RESOLVED_COMMIT" =~ ^[0-9a-fA-F]{40}$ ]] || die 'Unable to resolve build commit before release copy. Set RHMCP_RESOLVED_COMMIT for archive installs. / 复制 release 前无法确定完整 commit。归档安装请提供 RHMCP_RESOLVED_COMMIT。'
  [[ -n "$REQUESTED_REF" ]] || REQUESTED_REF="$RESOLVED_COMMIT"
  RELEASE_ID="${RMCP_VERSION}-${RESOLVED_COMMIT:0:7}-$(date +%Y%m%d%H%M%S)"
  export REQUESTED_REF RESOLVED_COMMIT RELEASE_ID
}

write_release_metadata() {
  local release="$1" tmp="$release/.rhmcp-release.env.tmp.$$" final="$release/.rhmcp-release.env"
  umask 077
  {
    printf 'RHMCP_RELEASE_VERSION=%s\n' "$(state_escape "$RMCP_VERSION")"
    printf 'RHMCP_RELEASE_ID=%s\n' "$(state_escape "$RELEASE_ID")"
    printf 'RHMCP_BUILD_COMMIT=%s\n' "$(state_escape "$RESOLVED_COMMIT")"
    printf 'RHMCP_BUILD_REF=%s\n' "$(state_escape "$REQUESTED_REF")"
  } > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$final"
}

write_progress_state() {
  local status="$1" stage="$2" error_class="${3:-}" tmp="$PROGRESS_STATE.tmp.$$"
  umask 077
  {
    printf 'INSTALL_STATUS=%s\n' "$(state_escape "$status")"
    printf 'LAST_COMPLETED_STAGE=%s\n' "$(state_escape "$stage")"
    printf 'TARGET_VERSION=%s\n' "$(state_escape "$RMCP_VERSION")"
    printf 'TARGET_COMMIT=%s\n' "$(state_escape "${RESOLVED_COMMIT:-unknown}")"
    printf 'TARGET_REF=%s\n' "$(state_escape "${REQUESTED_REF:-unknown}")"
    printf 'TARGET_RELEASE_ID=%s\n' "$(state_escape "${RELEASE_ID:-unknown}")"
    printf 'LAST_ERROR_CLASS=%s\n' "$(state_escape "$error_class")"
  } > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$PROGRESS_STATE"
}

load_progress_state() {
  [[ -f "$PROGRESS_STATE" ]] || return 1
  # Installer-owned root/user metadata only.
  # shellcheck disable=SC1090
  source "$PROGRESS_STATE"
}

ownership_key() {
  printf '%s' "$1" | tr '[:lower:]-./' '[:upper:]____' | tr -cd 'A-Z0-9_'
}

record_resource() {
  local name="$1" path="$2" ownership="$3" key tmp
  case "$ownership" in created|reused|shared) ;; *) die "Invalid ownership class: $ownership" ;; esac
  key="$(ownership_key "$name")"
  tmp="$OWNERSHIP_STATE.tmp.$$"
  umask 077
  if [[ -f "$OWNERSHIP_STATE" ]]; then
    grep -Ev "^RESOURCE_${key}_(PATH|OWNERSHIP)=" "$OWNERSHIP_STATE" > "$tmp" || true
  else
    : > "$tmp"
  fi
  {
    printf 'RESOURCE_%s_PATH=%s\n' "$key" "$(state_escape "$path")"
    printf 'RESOURCE_%s_OWNERSHIP=%s\n' "$key" "$(state_escape "$ownership")"
  } >> "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$OWNERSHIP_STATE"
}

resource_value() {
  local name="$1" field="$2" key var
  [[ -f "$OWNERSHIP_STATE" ]] || return 1
  key="$(ownership_key "$name")"; var="RESOURCE_${key}_${field}"
  # shellcheck disable=SC1090
  source "$OWNERSHIP_STATE"
  printf '%s\n' "${!var:-}"
}

resource_owned() { [[ "$(resource_value "$1" OWNERSHIP 2>/dev/null || true)" == created ]]; }

write_install_state() {
  local tmp="$INSTALL_STATE.tmp.$$"
  umask 077
  {
    printf 'RHMCP_INSTALL_STATUS=COMPLETE\n'
    printf 'RHMCP_INSTALL_VERSION=%s\n' "$(state_escape "$RMCP_VERSION")"
    printf 'RHMCP_BUILD_COMMIT=%s\n' "$(state_escape "$RESOLVED_COMMIT")"
    printf 'RHMCP_BUILD_REF=%s\n' "$(state_escape "$REQUESTED_REF")"
    printf 'RHMCP_RELEASE_ID=%s\n' "$(state_escape "$RELEASE_ID")"
    printf 'RHMCP_INSTALL_MODE=%s\n' "$(state_escape "$INSTALL_MODE")"
    printf 'RHMCP_CODE_BASE=%s\n' "$(state_escape "$CODE_BASE")"
    printf 'RHMCP_CONFIG_DIR=%s\n' "$(state_escape "$CONFIG_DIR")"
    printf 'RHMCP_STATE_DIR_PERSIST=%s\n' "$(state_escape "$STATE_DIR")"
    printf 'RHMCP_LOG_DIR=%s\n' "$(state_escape "$LOG_DIR")"
    printf 'RHMCP_SECRET_DIR_PERSIST=%s\n' "$(state_escape "$SECRET_DIR")"
    printf 'RHMCP_RUNTIME_DIR_PERSIST=%s\n' "$(state_escape "$RUNTIME_DIR")"
    printf 'RHMCP_OWNERSHIP_STATE=%s\n' "$(state_escape "$OWNERSHIP_STATE")"
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
  # State is installer-owned metadata, never arbitrary remote input.
  # shellcheck disable=SC1090
  source "$file"
}
