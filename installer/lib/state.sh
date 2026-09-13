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
  RESOLVED_COMMIT="${RESOLVED_COMMIT,,}"
  [[ -n "$REQUESTED_REF" ]] || REQUESTED_REF="$RESOLVED_COMMIT"
  RELEASE_ID="${RMCP_VERSION}-${RESOLVED_COMMIT:0:7}-$(date +%Y%m%d%H%M%S)"
  export REQUESTED_REF RESOLVED_COMMIT RELEASE_ID
}

write_release_metadata() {
  local release_dir tmp final
  release_dir="$1"
  tmp="$release_dir/.rhmcp-release.env.tmp.$$"
  final="$release_dir/.rhmcp-release.env"
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
    printf 'TARGET_INSTALL_MODE=%s\n' "$(state_escape "${INSTALL_MODE:-}")"
    printf 'TARGET_CODE_BASE=%s\n' "$(state_escape "${CODE_BASE:-}")"
    printf 'TARGET_AUTHORITY=%s\n' "$(state_escape "${AUTHORITY:-}")"
    printf 'TARGET_SERVICE_USER=%s\n' "$(state_escape "${SERVICE_USER:-}")"
    printf 'TARGET_INGRESS=%s\n' "$(state_escape "${INGRESS:-}")"
    printf 'TARGET_LOCAL_PORT=%s\n' "$(state_escape "${LOCAL_PORT:-}")"
    printf 'TARGET_PUBLIC_HOST=%s\n' "$(state_escape "${PUBLIC_HOST:-}")"
    printf 'TARGET_PUBLIC_HTTPS_PORT=%s\n' "$(state_escape "${PUBLIC_HTTPS_PORT:-443}")"
    printf 'TARGET_AUTH_MODE=%s\n' "$(state_escape "${AUTH_MODE:-}")"
    printf 'LAST_ERROR_CLASS=%s\n' "$(state_escape "$error_class")"
  } > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$PROGRESS_STATE"
  LAST_COMPLETED_STAGE="$stage"
  if [[ "$status" == COMPLETE && "$stage" == COMPLETE ]]; then
    if [[ -n "${PATH_KEY:-}" ]]; then PATH_KEY='<redacted>'; fi
    unset RHMCP_VALIDATION_BEARER_TOKEN 2>/dev/null || true
  fi
}

load_progress_state() {
  [[ -f "$PROGRESS_STATE" ]] || return 1
  # shellcheck disable=SC1090
  source "$PROGRESS_STATE"
}

read_progress_completed_stage() {
  local raw
  [[ -f "$PROGRESS_STATE" ]] || return 1
  raw="$(sed -n 's/^LAST_COMPLETED_STAGE=//p' "$PROGRESS_STATE" | head -n 1)"
  case "$raw" in
    NONE|PRECHECK|PREPARE|RELEASE|RUNTIME|CLI_RECOVERY|SERVICE|LOCAL_READY|INGRESS|TLS|PUBLIC_READY|MCP_VERIFY|COMPLETE)
      printf '%s\n' "$raw"
      ;;
    *)
      return 1
      ;;
  esac
}

restore_transaction_state() {
  load_progress_state || return 1
  [[ "$INSTALL_STATUS" == INCOMPLETE ]] || return 1
  [[ "${TARGET_COMMIT:-}" =~ ^[0-9a-fA-F]{40}$ ]] || return 1
  RESOLVED_COMMIT="${TARGET_COMMIT,,}"
  REQUESTED_REF="${TARGET_REF:-$RESOLVED_COMMIT}"
  RELEASE_ID="${TARGET_RELEASE_ID:-}"
  AUTHORITY="${TARGET_AUTHORITY:-$AUTHORITY}"
  SERVICE_USER="${TARGET_SERVICE_USER:-$SERVICE_USER}"
  INGRESS="${TARGET_INGRESS:-$INGRESS}"
  LOCAL_PORT="${TARGET_LOCAL_PORT:-$LOCAL_PORT}"
  PUBLIC_HOST="${TARGET_PUBLIC_HOST:-$PUBLIC_HOST}"
  PUBLIC_HTTPS_PORT="${TARGET_PUBLIC_HTTPS_PORT:-443}"
  AUTH_MODE="${TARGET_AUTH_MODE:-$AUTH_MODE}"
  export RESOLVED_COMMIT REQUESTED_REF RELEASE_ID AUTHORITY SERVICE_USER INGRESS LOCAL_PORT PUBLIC_HOST PUBLIC_HTTPS_PORT AUTH_MODE
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
  local status="${1:-COMPLETE}" tmp="$INSTALL_STATE.tmp.$$"
  umask 077
  {
    printf 'RHMCP_INSTALL_STATUS=%s\n' "$(state_escape "$status")"
    printf 'RHMCP_INSTALL_VERSION=%s\n' "$(state_escape "$RMCP_VERSION")"
    printf 'RHMCP_BUILD_COMMIT=%s\n' "$(state_escape "${RESOLVED_COMMIT:-unknown}")"
    printf 'RHMCP_BUILD_REF=%s\n' "$(state_escape "${REQUESTED_REF:-unknown}")"
    printf 'RHMCP_RELEASE_ID=%s\n' "$(state_escape "${RELEASE_ID:-unknown}")"
    printf 'RHMCP_INSTALL_MODE=%s\n' "$(state_escape "$INSTALL_MODE")"
    printf 'RHMCP_CODE_BASE=%s\n' "$(state_escape "$CODE_BASE")"
    printf 'RHMCP_CONFIG_DIR=%s\n' "$(state_escape "$CONFIG_DIR")"
    printf 'RHMCP_STATE_DIR_PERSIST=%s\n' "$(state_escape "$STATE_DIR")"
    printf 'RHMCP_LOG_DIR=%s\n' "$(state_escape "$LOG_DIR")"
    printf 'RHMCP_SECRET_DIR_PERSIST=%s\n' "$(state_escape "$SECRET_DIR")"
    printf 'RHMCP_RUNTIME_DIR_PERSIST=%s\n' "$(state_escape "$RUNTIME_DIR")"
    printf 'RHMCP_BACKUP_DIR=%s\n' "$(state_escape "$BACKUP_DIR")"
    printf 'RHMCP_OWNERSHIP_STATE=%s\n' "$(state_escape "$OWNERSHIP_STATE")"
    printf 'RHMCP_PROGRESS_STATE=%s\n' "$(state_escape "$PROGRESS_STATE")"
    printf 'RHMCP_LANGUAGE=%s\n' "$(state_escape "$RMCP_LANGUAGE")"
    printf 'RHMCP_AUTHORITY=%s\n' "$(state_escape "$AUTHORITY")"
    printf 'RHMCP_INGRESS=%s\n' "$(state_escape "$INGRESS")"
    printf 'RHMCP_LOCAL_PORT=%s\n' "$(state_escape "$LOCAL_PORT")"
    printf 'RHMCP_PUBLIC_HOST_STATE=%s\n' "$(state_escape "$PUBLIC_HOST")"
    printf 'RHMCP_PUBLIC_HTTPS_PORT=%s\n' "$(state_escape "${PUBLIC_HTTPS_PORT:-443}")"
    printf 'RHMCP_SERVICE_BACKEND=%s\n' "$(state_escape "$SERVICE_BACKEND")"
  } > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$INSTALL_STATE"
}

load_install_state() {
  local file="$1"
  [[ -f "$file" ]] || return 1
  # shellcheck disable=SC1090
  source "$file"
}