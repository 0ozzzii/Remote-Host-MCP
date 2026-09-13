#!/usr/bin/env bash
set -euo pipefail

resolve_root() {
  if [[ -n "${RMCP_CONTROLLER_ROOT:-}" && -d "$RMCP_CONTROLLER_ROOT" && -f "$RMCP_CONTROLLER_ROOT/installer/install.sh" ]]; then
    printf '%s\n' "$RMCP_CONTROLLER_ROOT"
    return
  fi
  if [[ -n "${RMCP_INSTALL_STATE:-}" && -f "$RMCP_INSTALL_STATE" ]]; then
    # shellcheck disable=SC1090
    source "$RMCP_INSTALL_STATE"
    printf '%s/current\n' "$RHMCP_CODE_BASE"
    return
  fi
  cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd
}

ROOT="$(resolve_root)"
export RMCP_ROOT="$ROOT"
LIB_ROOT="$ROOT/installer"
STATE_FILE="${RMCP_INSTALL_STATE:-}"

# shellcheck disable=SC1091
source "$LIB_ROOT/lib/i18n.sh"
# shellcheck disable=SC1091
source "$LIB_ROOT/lib/common.sh"
# shellcheck disable=SC1091
source "$LIB_ROOT/lib/ports.sh"
# shellcheck disable=SC1091
source "$LIB_ROOT/lib/paths.sh"
# shellcheck disable=SC1091
source "$LIB_ROOT/lib/state.sh"
# shellcheck disable=SC1091
source "$LIB_ROOT/lib/readiness.sh"
# shellcheck disable=SC1091
source "$LIB_ROOT/lib/lifecycle.sh"
# shellcheck disable=SC1091
source "$LIB_ROOT/lib/external_probe.sh"
# shellcheck disable=SC1091
source "$LIB_ROOT/lib/uninstall.sh"
# shellcheck disable=SC1091
source "$LIB_ROOT/lib/update.sh"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib.sh"

VERSION="$(tr -d '\r\n' < "$ROOT/VERSION" 2>/dev/null || printf 'unknown')"
if [[ -n "$STATE_FILE" && -f "$STATE_FILE" ]]; then
  load_install_layout_from_state "$STATE_FILE" || die 'Install state contains an unsafe or invalid layout.'
  load_locale "${RHMCP_LANGUAGE:-en_US}"
else
  load_locale "${RHMCP_LANGUAGE:-en_US}"
fi

usage() {
  cat <<'EOF'
Usage: rmcp <command> [options]

Lifecycle commands:
  status                    Show secret-free installation/service/TLS status
  doctor                    Run layered runtime, ingress, MCP and renewal checks
  logs [LINES]              Show redacted service logs (default 120)
  restart                   Restart Remote Host MCP and verify bounded readiness
  repair                    Repair the local lifecycle without replacing healthy shared resources
  resume                    Resume an INCOMPLETE installer transaction at the same commit
  uninstall --dry-run       Show exactly what owned resources would be removed
  uninstall                 Remove application/runtime/service/CLI, preserve recovery config/secrets/backups
  uninstall --purge         Remove the entire Remote Host MCP-owned namespace after confirmation
  stray-check [--purge]     Verify known lifecycle residues
  check-update              Compare current version with the update channel
  connection                Explicitly show connection details; capability URL is a credential
  version                   Print version/build identity

Compatibility:
  --root, --version, --help
EOF
}

require_state() {
  [[ -n "$STATE_FILE" && -f "$STATE_FILE" ]] || die 'Install state is unavailable. Run this command through the installed rmcp launcher.'
}

service_status() {
  if [[ "${RHMCP_SERVICE_BACKEND:-portable}" == systemd ]] && command -v systemctl >/dev/null 2>&1; then
    systemctl is-active remote-host-mcp.service 2>/dev/null || true
  else
    bash "$CURRENT_LINK/scripts/manage.sh" status 2>/dev/null | head -1 || true
  fi
}

service_action() {
  local action="$1"
  if [[ "${RHMCP_SERVICE_BACKEND:-portable}" == systemd ]] && command -v systemctl >/dev/null 2>&1; then
    systemctl "$action" remote-host-mcp.service
  else
    case "$action" in start|stop|restart) bash "$CURRENT_LINK/scripts/manage.sh" "$action" ;; *) return 2 ;; esac
  fi
}

redact_stream() {
  sed -E \
    -e 's#(https?://[^/[:space:]]+/mcp/)[A-Za-z0-9._~+/=-]+#\1<redacted>#g' \
    -e 's#([Tt]oken|[Kk]ey|[Ss]ecret)([= :]+)[^[:space:]]+#\1\2<redacted>#g' \
    -e 's#(RHMCP_PATH_KEY=)[^[:space:]]+#\1<redacted>#g'
}

status_cmd() {
  require_state
  local port host https_port current cert_file cert_expiry='n/a' timer='n/a'
  port="${RHMCP_LOCAL_PORT:-8765}"
  host="${RHMCP_PUBLIC_HOST_STATE:-mcp.invalid}"
  https_port="${RHMCP_PUBLIC_HTTPS_PORT:-443}"
  current="$(readlink -f "$CURRENT_LINK" 2>/dev/null || printf 'missing')"
  cert_file="$(get_env_value RHMCP_CERT_FULLCHAIN 2>/dev/null || true)"
  if [[ -n "$cert_file" && -r "$cert_file" ]] && command -v openssl >/dev/null 2>&1; then
    cert_expiry="$(openssl x509 -in "$cert_file" -noout -enddate 2>/dev/null | sed 's/^notAfter=//' || true)"
  fi
  if [[ -n "$(resource_value cert_renew_timer PATH 2>/dev/null || true)" ]] && command -v systemctl >/dev/null 2>&1; then
    timer="$(systemctl is-enabled remote-host-mcp-cert-renew.timer 2>/dev/null || true)/$(systemctl is-active remote-host-mcp-cert-renew.timer 2>/dev/null || true)"
  fi
  header "Remote Host MCP ${RHMCP_INSTALL_VERSION:-$VERSION}"
  printf 'Install status       : %s\n' "${RHMCP_INSTALL_STATUS:-unknown}"
  printf 'Build commit         : %s\n' "${RHMCP_BUILD_COMMIT:-unknown}"
  printf 'Build ref            : %s\n' "${RHMCP_BUILD_REF:-unknown}"
  printf 'Current release      : %s\n' "$current"
  printf 'Service              : %s (%s)\n' "$(service_status | head -1)" "${RHMCP_SERVICE_BACKEND:-unknown}"
  printf 'Local endpoint       : http://127.0.0.1:%s\n' "$port"
  printf 'Ingress              : %s\n' "${RHMCP_INGRESS:-unknown}"
  if [[ "${RHMCP_INGRESS:-private}" != private ]]; then
    printf 'Public endpoint      : https://%s%s\n' "$host" "$([[ "$https_port" == 443 ]] && printf '' || printf ':%s' "$https_port")"
  fi
  printf 'Certificate expiry   : %s\n' "$cert_expiry"
  printf 'Renewal timer        : %s\n' "$timer"
}

_mcp_validation_url() {
  load_env || return 1
  local port host https_port auth key base
  port="$(mcp_env_value PORT 2>/dev/null || printf '8765')"
  host="${RHMCP_PUBLIC_HOST_STATE:-$(mcp_env_value PUBLIC_HOST 2>/dev/null || true)}"
  https_port="${RHMCP_PUBLIC_HTTPS_PORT:-443}"
  auth="$(mcp_env_value AUTH_MODE 2>/dev/null || printf 'capability')"
  if [[ "${RHMCP_INGRESS:-private}" == private ]]; then base="http://127.0.0.1:${port}"; else
    if [[ "$https_port" == 443 ]]; then base="https://${host}"; else base="https://${host}:${https_port}"; fi
  fi
  if [[ "$auth" == capability ]]; then
    key="$(mcp_env_value PATH_KEY 2>/dev/null || true)"; [[ -n "$key" ]] || return 1
    printf '%s/mcp/%s\n' "$base" "$key"
  else
    printf '%s/mcp\n' "$base"
  fi
}

doctor_cmd() {
  require_state
  local failures=0 port url rc cert_file bearer="${RHMCP_VALIDATION_BEARER_TOKEN:-}"
  port="${RHMCP_LOCAL_PORT:-8765}"
  header 'Remote Host MCP doctor'
  if [[ -L "$CURRENT_LINK" && -x "$CURRENT_LINK/.venv/bin/remote-host-mcp" ]]; then ok 'runtime/current release'; else fail 'runtime/current release missing'; failures=$((failures+1)); fi
  if wait_local_health "$port" 3 1; then ok 'local health'; else fail 'local health'; failures=$((failures+1)); fi

  if [[ "${RHMCP_INGRESS:-private}" != private ]]; then
    local host="${RHMCP_PUBLIC_HOST_STATE:-}" https_port="${RHMCP_PUBLIC_HTTPS_PORT:-443}" health_url
    if [[ "$https_port" == 443 ]]; then health_url="https://${host}/health"; else health_url="https://${host}:${https_port}/health"; fi
    set +e; external_http_probe "$health_url" 200; rc=$?; set -e
    if [[ $rc -eq 0 ]]; then ok 'external HTTPS health'; else fail "external HTTPS health (${EXTERNAL_PROBE_BLOCK_HINT:-probe failed})"; failures=$((failures+1)); fi
  else
    info 'public ingress check skipped: private/local profile'
  fi

  cert_file="$(get_env_value RHMCP_CERT_FULLCHAIN 2>/dev/null || true)"
  if [[ -n "$cert_file" ]]; then
    if [[ -r "$cert_file" ]] && openssl x509 -checkend 86400 -noout -in "$cert_file" >/dev/null 2>&1; then ok 'certificate readable and not expiring within 24h'; else fail 'certificate missing/near expiry'; failures=$((failures+1)); fi
  fi

  url="$(_mcp_validation_url 2>/dev/null || true)"
  if [[ -n "$url" ]]; then
    load_env || true
    if [[ "$(mcp_env_value AUTH_MODE 2>/dev/null || printf capability)" == oauth && -z "$bearer" ]]; then
      warn 'MCP protocol validation skipped for OAuth: set RHMCP_VALIDATION_BEARER_TOKEN for this one command.'
    else
      set +e
      RHMCP_VALIDATE_URL="$url" RHMCP_VALIDATION_BEARER_TOKEN="$bearer" RHMCP_VALIDATE_TOOL_COUNT=65 \
        "$CURRENT_LINK/.venv/bin/python" "$ROOT/installer/validate_mcp.py" >/dev/null
      rc=$?
      set -e
      if [[ $rc -eq 0 ]]; then ok 'MCP initialize/tools-list/host_capabilities'; else fail 'MCP protocol gate'; failures=$((failures+1)); fi
    fi
  else
    fail 'could not construct MCP validation endpoint'; failures=$((failures+1))
  fi

  if [[ -n "$(resource_value cert_renew_timer PATH 2>/dev/null || true)" ]]; then
    if command -v systemctl >/dev/null 2>&1 && systemctl is-enabled remote-host-mcp-cert-renew.timer >/dev/null 2>&1; then ok 'certificate renewal timer enabled'; else fail 'certificate renewal timer not enabled'; failures=$((failures+1)); fi
  fi

  if (( failures == 0 )); then ok 'doctor PASS'; return 0; fi
  fail "doctor found ${failures} failing layer(s)"
  return 1
}

logs_cmd() {
  local lines="${1:-120}"
  [[ "$lines" =~ ^[0-9]+$ && "$lines" -ge 1 && "$lines" -le 2000 ]] || die 'LINES must be 1..2000'
  if [[ "${RHMCP_SERVICE_BACKEND:-portable}" == systemd ]] && command -v journalctl >/dev/null 2>&1; then
    journalctl -u remote-host-mcp.service -n "$lines" --no-pager 2>&1 | redact_stream
  else
    safe_tail_file "$LOG_DIR/server.log" "$lines"
  fi
}

restart_cmd() {
  require_state
  service_action restart >/dev/null
  if wait_local_health "${RHMCP_LOCAL_PORT:-8765}" 30 1; then ok 'restart + readiness PASS'; else service_readiness_diagnostics "${RHMCP_LOCAL_PORT:-8765}"; return 1; fi
}

export_install_provenance() {
  export RHMCP_REQUESTED_REF="${RHMCP_BUILD_REF:-}"
  export RHMCP_RESOLVED_COMMIT="${RHMCP_BUILD_COMMIT:-}"
  export RMCP_INSTALL_STATE="$STATE_FILE"
}

repair_cmd() {
  require_state
  export_install_provenance
  exec bash "$ROOT/installer/install.sh" --repair
}

resume_cmd() {
  require_state
  [[ "${RHMCP_INSTALL_STATUS:-}" == INCOMPLETE ]] || die 'Install state is not INCOMPLETE.'
  export_install_provenance
  exec bash "$ROOT/installer/install.sh" --resume
}

uninstall_cmd() {
  require_state
  local purge=false dry=false arg
  shift || true
  for arg in "$@"; do
    case "$arg" in --dry-run) dry=true ;; --purge) purge=true ;; *) die "Unknown uninstall option: $arg" ;; esac
  done
  uninstall_plan "$purge"
  [[ "$dry" == true ]] && return 0
  if [[ "${RHMCP_ASSUME_YES:-0}" != 1 ]]; then confirm 'Apply this ownership-aware uninstall plan?' || { info 'Cancelled'; return 0; }; fi
  uninstall_apply "$purge"
  stray_check "$purge"
  ok 'Uninstall completed and stray check passed.'
}

show_connection() {
  load_env || die 'Missing configuration / 缺少配置'
  local host key auth port https_port
  host="$(mcp_env_value PUBLIC_HOST 2>/dev/null || true)"
  key="$(mcp_env_value PATH_KEY 2>/dev/null || true)"
  auth="$(mcp_env_value AUTH_MODE 2>/dev/null || printf 'capability')"
  port="$(mcp_env_value PORT 2>/dev/null || printf '8765')"
  https_port="${RHMCP_PUBLIC_HTTPS_PORT:-443}"
  header "Remote Host MCP $VERSION"
  printf 'Local / 本地       : http://127.0.0.1:%s\n' "$port"
  if [[ "${RHMCP_INGRESS:-private}" != private ]]; then printf 'Public host / 域名 : %s\n' "$host"; fi
  if [[ "$auth" == oauth ]]; then
    printf 'Auth / 认证        : OAuth 2.1\n'
    if [[ "${RHMCP_INGRESS:-private}" == private ]]; then printf 'MCP URL            : http://127.0.0.1:%s/mcp\n' "$port"; else printf 'MCP URL            : https://%s%s/mcp\n' "$host" "$([[ "$https_port" == 443 ]] && printf '' || printf ':%s' "$https_port")"; fi
  else
    [[ -n "$key" ]] || { warn 'Capability key is missing / 私密连接密钥缺失'; return 1; }
    printf 'Auth / 认证        : private capability URL\n'
    if [[ "${RHMCP_INGRESS:-private}" == private ]]; then printf 'MCP URL            : http://127.0.0.1:%s/mcp/%s\n' "$port" "$key"; else printf 'MCP URL            : https://%s%s/mcp/%s\n' "$host" "$([[ "$https_port" == 443 ]] && printf '' || printf ':%s' "$https_port")" "$key"; fi
    warn 'The URL contains a credential. Treat it like a password. / 完整 URL 含访问密钥，请像密码一样保存。'
  fi
}

check_update_cmd() {
  local remote
  remote="$(latest_version || true)"
  [[ -n "$remote" ]] || { warn 'Could not query update channel / 无法查询更新通道'; return 1; }
  printf 'Current / 当前: %s\nLatest / 最新: %s\n' "$VERSION" "$remote"
  if version_is_newer "$VERSION" "$remote"; then warn "$(t update_available): $remote"; else ok "$(t update_none)"; fi
}

interactive_menu() {
  local choice
  while true; do
    clear 2>/dev/null || true
    header "Remote Host MCP $VERSION"
    printf 'Status / 状态 : %s\n' "$(service_status | head -1)"
    printf 'Root / 控制器 : %s\n' "$ROOT"
    printf 'Runtime / 当前: %s\n' "$(readlink -f "$CURRENT_LINK" 2>/dev/null || printf missing)"
    printf 'Ingress / 接入: %s\n' "${RHMCP_INGRESS:-legacy/source}"
    subhr
    if [[ "$RMCP_LANGUAGE" == zh_CN ]]; then
      printf '  1. 状态\n  2. 完整诊断\n  3. 查看连接信息\n  4. 重启服务\n  5. 查看日志\n  6. 修复\n  7. 检查更新\n  8. 卸载预览\n  0. 退出\n'
    else
      printf '  1. Status\n  2. Doctor\n  3. Connection information\n  4. Restart service\n  5. Logs\n  6. Repair\n  7. Check update\n  8. Uninstall dry-run\n  0. Exit\n'
    fi
    subhr
    read -r -p 'Select / 选择 [0-8]: ' choice
    case "$choice" in
      1) status_cmd ;;
      2) doctor_cmd || true ;;
      3) show_connection ;;
      4) restart_cmd || true ;;
      5) logs_cmd 120 ;;
      6) repair_cmd ;;
      7) check_update_cmd || true ;;
      8) uninstall_plan false ;;
      0) exit 0 ;;
      *) warn 'Invalid selection / 无效选项' ;;
    esac
    printf '\n'; read -r -p "$(t press_enter)" _ || true
  done
}

case "${1:-}" in
  --root) printf '%s\n' "$ROOT" ;;
  --version|-V|version) printf 'Remote Host MCP %s commit=%s ref=%s\n' "$VERSION" "${RHMCP_BUILD_COMMIT:-unknown}" "${RHMCP_BUILD_REF:-unknown}" ;;
  --help|-h|help) usage ;;
  status) status_cmd ;;
  doctor) doctor_cmd ;;
  logs) shift; logs_cmd "${1:-120}" ;;
  restart) restart_cmd ;;
  repair) repair_cmd ;;
  resume) resume_cmd ;;
  uninstall) uninstall_cmd "$@" ;;
  stray-check)
    shift
    if [[ -f "${OWNERSHIP_STATE:-}" ]]; then cache_uninstall_resources; fi
    stray_check "$([[ "${1:-}" == --purge ]] && printf true || printf false)"
    ;;
  check-update) check_update_cmd ;;
  connection) show_connection ;;
  '') interactive_menu ;;
  *) fail "Unknown rmcp command: $1"; usage >&2; exit 2 ;;
esac
