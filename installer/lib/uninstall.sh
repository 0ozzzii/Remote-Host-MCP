#!/usr/bin/env bash
set -euo pipefail

_uninstall_print() { printf '  %-8s %s\n' "$1" "$2"; }

cache_uninstall_resources() {
  UNINSTALL_CHECK_CLI="$(resource_value rmcp_cli PATH 2>/dev/null || true)"
  UNINSTALL_CHECK_UNIT="$(resource_value service_unit PATH 2>/dev/null || true)"
  UNINSTALL_CHECK_REPORT_UNIT="$(resource_value report_service_unit PATH 2>/dev/null || true)"
  UNINSTALL_CHECK_TUNNEL_UNIT="$(resource_value tunnel_service_unit PATH 2>/dev/null || true)"
  UNINSTALL_CHECK_RENEW_SERVICE="$(resource_value cert_renew_service PATH 2>/dev/null || true)"
  UNINSTALL_CHECK_RENEW_TIMER="$(resource_value cert_renew_timer PATH 2>/dev/null || true)"
  UNINSTALL_CHECK_RENEW_PID="$(resource_value portable_cert_renew_pid PATH 2>/dev/null || true)"
  UNINSTALL_CHECK_RENEW_LOOP="$(resource_value cert_renew_loop PATH 2>/dev/null || true)"
  UNINSTALL_CHECK_NGINX_SITE="$(resource_value nginx_site PATH 2>/dev/null || true)"
  UNINSTALL_CHECK_NGINX_LINK="$(resource_value nginx_site_link PATH 2>/dev/null || true)"
  UNINSTALL_CHECK_PORT="${RHMCP_LOCAL_PORT:-${LOCAL_PORT:-8765}}"
  export UNINSTALL_CHECK_CLI UNINSTALL_CHECK_UNIT UNINSTALL_CHECK_REPORT_UNIT UNINSTALL_CHECK_TUNNEL_UNIT UNINSTALL_CHECK_RENEW_SERVICE UNINSTALL_CHECK_RENEW_TIMER UNINSTALL_CHECK_RENEW_PID UNINSTALL_CHECK_RENEW_LOOP UNINSTALL_CHECK_NGINX_SITE UNINSTALL_CHECK_NGINX_LINK UNINSTALL_CHECK_PORT
}

uninstall_plan() {
  local purge="${1:-false}" cli unit tunnel_unit renew_service renew_timer renew_pid renew_loop nginx_site nginx_link cert_name
  cli="$(resource_value rmcp_cli PATH 2>/dev/null || true)"
  unit="$(resource_value service_unit PATH 2>/dev/null || true)"
  report_unit="$(resource_value report_service_unit PATH 2>/dev/null || true)"
  tunnel_unit="$(resource_value tunnel_service_unit PATH 2>/dev/null || true)"
  renew_service="$(resource_value cert_renew_service PATH 2>/dev/null || true)"
  renew_timer="$(resource_value cert_renew_timer PATH 2>/dev/null || true)"
  renew_pid="$(resource_value portable_cert_renew_pid PATH 2>/dev/null || true)"
  renew_loop="$(resource_value cert_renew_loop PATH 2>/dev/null || true)"
  nginx_site="$(resource_value nginx_site PATH 2>/dev/null || true)"
  nginx_link="$(resource_value nginx_site_link PATH 2>/dev/null || true)"
  cert_name="$(resource_value certificate PATH 2>/dev/null || true)"
  header 'Remote Host MCP uninstall plan / 卸载计划'
  _uninstall_print REMOVE "application releases/current under: ${CODE_BASE:-unknown}"
  _uninstall_print REMOVE "runtime state: ${STATE_DIR:-unknown}"
  _uninstall_print REMOVE "product runtime: ${RUNTIME_DIR:-unknown}"
  [[ -n "$cli" ]] && _uninstall_print REMOVE "owned rmcp CLI: $cli"
  [[ -n "$unit" ]] && _uninstall_print REMOVE "owned service unit: $unit"
  [[ -n "$report_unit" ]] && _uninstall_print REMOVE "owned report service unit: $report_unit"
  [[ -n "$tunnel_unit" ]] && _uninstall_print REMOVE "owned tunnel unit: $tunnel_unit"
  [[ -n "$renew_service" ]] && _uninstall_print REMOVE "owned certificate renewal service: $renew_service"
  [[ -n "$renew_timer" ]] && _uninstall_print REMOVE "owned certificate renewal timer: $renew_timer"
  [[ -n "$renew_pid" ]] && _uninstall_print REMOVE "owned portable certificate renewal process: $renew_pid"
  [[ -n "$renew_loop" ]] && _uninstall_print REMOVE "owned portable certificate renewal loop: $renew_loop"
  [[ -n "$nginx_link" ]] && _uninstall_print REMOVE "owned Nginx enable link: $nginx_link"
  [[ -n "$nginx_site" ]] && _uninstall_print REMOVE "owned ingress file: $nginx_site"
  _uninstall_print KEEP 'system Nginx/Caddy/Apache, Certbot/Python packages and unrelated services'
  _uninstall_print KEEP 'reused/shared resources and unrelated certificates/DNS records'
  if [[ "$purge" == true ]]; then
    _uninstall_print REMOVE "owned product config/secrets/hooks: ${CONFIG_DIR:-unknown} / ${SECRET_DIR:-unknown}"
    _uninstall_print REMOVE "owned logs/backups: ${LOG_DIR:-unknown} / ${BACKUP_DIR:-unknown}"
    if [[ -n "$cert_name" ]] && resource_owned certificate; then
      if [[ "${RHMCP_PURGE_CERTIFICATES:-0}" == 1 ]]; then
        _uninstall_print REMOVE "owned certificate (explicit opt-in): $cert_name"
      else
        _uninstall_print KEEP "owned certificate metadata until namespace purge; explicit certbot delete requires RHMCP_PURGE_CERTIFICATES=1: $cert_name"
      fi
    fi
  else
    _uninstall_print KEEP 'config, DNS/Tunnel credentials, certificate material, logs and backups for recovery; use --purge for namespace removal'
  fi
}

_disable_owned_systemd_resource() {
  local name="$1" path
  path="$(resource_value "$name" PATH 2>/dev/null || true)"
  [[ -n "$path" ]] || return 0
  resource_owned "$name" || return 0
  command -v systemctl >/dev/null 2>&1 || return 0
  systemctl disable --now "$(basename "$path")" >/dev/null 2>&1 || true
}

_wait_port_release() {
  local port="$1" tries="${2:-50}" i
  [[ "$port" =~ ^[0-9]+$ ]] || return 2
  for ((i=0; i<tries; i++)); do
    port_listening "$port" || return 0
    sleep 0.1
  done
  return 1
}

_stop_owned_services() {
  local port="${RHMCP_LOCAL_PORT:-${LOCAL_PORT:-8765}}" renew_pid renew_loop
  _disable_owned_systemd_resource cert_renew_timer
  _disable_owned_systemd_resource report_service_unit
  _disable_owned_systemd_resource tunnel_service_unit
  _disable_owned_systemd_resource service_unit
  renew_pid="$(resource_value portable_cert_renew_pid PATH 2>/dev/null || true)"
  renew_loop="$(resource_value cert_renew_loop PATH 2>/dev/null || true)"
  if [[ -n "$renew_pid" ]] && resource_owned portable_cert_renew_pid; then
    if declare -F stop_portable_renewal >/dev/null 2>&1; then
      stop_portable_renewal "$renew_pid" "${renew_loop:-$CERTBOT_DIR/renew-loop.sh}" || {
        fail 'Portable certificate renewal stop failed; refusing destructive cleanup while process identity is unresolved.'
        return 1
      }
    elif [[ -f "$renew_pid" ]]; then
      fail 'Portable certificate renewal is recorded but its identity-aware stop helper is unavailable; refusing destructive cleanup.'
      return 1
    fi
  fi
  if [[ "${RHMCP_SERVICE_BACKEND:-}" == portable && -x "${CURRENT_LINK:-}/scripts/manage.sh" ]]; then
    (cd "$CURRENT_LINK" && bash scripts/manage.sh tunnel-stop >/dev/null 2>&1) || true
    if ! (cd "$CURRENT_LINK" && bash scripts/manage.sh stop); then
      fail 'Portable server stop failed; refusing to remove its release/runtime while process identity is unresolved.'
      return 1
    fi
    if ! _wait_port_release "$port"; then
      fail "Portable server still accepts connections on local port ${port}; refusing destructive cleanup."
      return 1
    fi
  fi
}

_remove_owned_file_resource() {
  local name="$1" path
  path="$(resource_value "$name" PATH 2>/dev/null || true)"
  [[ -n "$path" ]] || return 0
  resource_owned "$name" || return 0
  [[ "$path" = /* ]] || return 1
  rm -f -- "$path"
}

_remove_owned_certificate_via_certbot() {
  local cert certbot config_dir work_dir logs_dir
  [[ "${RHMCP_PURGE_CERTIFICATES:-0}" == 1 ]] || return 0
  resource_owned certificate || return 0
  cert="$(resource_value certificate PATH 2>/dev/null || true)"
  [[ -n "$cert" && "$cert" != */* ]] || return 1
  certbot="${RUNTIME_DIR}/certbot-venv/bin/certbot"
  config_dir="${CONFIG_DIR}/letsencrypt"
  work_dir="${STATE_DIR}/certbot-work"
  logs_dir="${LOG_DIR}/certbot"
  [[ -x "$certbot" && -d "$config_dir" ]] || return 0
  "$certbot" --config-dir "$config_dir" --work-dir "$work_dir" --logs-dir "$logs_dir" \
    delete --cert-name "$cert" --non-interactive >/dev/null 2>&1 || return 1
}

_reload_shared_nginx_after_removal() {
  command -v nginx >/dev/null 2>&1 || return 0
  nginx -t >/dev/null 2>&1 || { warn 'Nginx config test failed after product config removal; not reloading.'; return 1; }
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active nginx >/dev/null 2>&1; then
    systemctl reload nginx >/dev/null 2>&1 || return 1
  elif pgrep -x nginx >/dev/null 2>&1; then
    nginx -s reload >/dev/null 2>&1 || return 1
  fi
}

mark_uninstalled_state() {
  local tmp
  [[ -n "${INSTALL_STATE:-}" && -f "$INSTALL_STATE" ]] || return 0
  tmp="${INSTALL_STATE}.tmp.$$"
  awk 'BEGIN{done=0} /^RHMCP_INSTALL_STATUS=/ {print "RHMCP_INSTALL_STATUS=UNINSTALLED"; done=1; next} {print} END{if(!done) print "RHMCP_INSTALL_STATUS=UNINSTALLED"}' "$INSTALL_STATE" > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$INSTALL_STATE"
}

remove_application_payload() {
  [[ -n "${CURRENT_LINK:-}" && "$CURRENT_LINK" = /* ]] && rm -f -- "$CURRENT_LINK"
  [[ -n "${RELEASES_DIR:-}" && "$RELEASES_DIR" = /* && -d "$RELEASES_DIR" ]] && rm -rf -- "$RELEASES_DIR"
  [[ -n "${STATE_DIR:-}" && "$STATE_DIR" = /* && -d "$STATE_DIR" ]] && rm -rf -- "$STATE_DIR"
  [[ -n "${RUNTIME_DIR:-}" && "$RUNTIME_DIR" = /* && -d "$RUNTIME_DIR" ]] && rm -rf -- "$RUNTIME_DIR"
  if [[ "${INSTALL_MODE:-}" == system && -n "${CODE_BASE:-}" && "$CODE_BASE" = /* ]]; then
    rmdir "$CODE_BASE" >/dev/null 2>&1 || true
  fi
}

_safe_purge_dir() {
  local path="$1"
  [[ -n "$path" && "$path" = /* && "$path" != / ]] || return 1
  [[ -d "$path" ]] && rm -rf -- "$path"
}

uninstall_apply() {
  local purge="${1:-false}"
  [[ -n "${CODE_BASE:-}" && "$CODE_BASE" = /* && "$CODE_BASE" != / ]] || die 'Unsafe or missing CODE_BASE; refusing uninstall.'
  [[ -n "${CONFIG_DIR:-}" && "$CONFIG_DIR" = /* && "$CONFIG_DIR" != / ]] || die 'Unsafe or missing CONFIG_DIR; refusing uninstall.'
  cache_uninstall_resources
  uninstall_plan "$purge"
  _stop_owned_services || die 'Failed to stop Remote Host MCP-owned services; uninstall aborted before payload removal.'
  _remove_owned_file_resource cert_renew_timer
  _remove_owned_file_resource cert_renew_service
  _remove_owned_file_resource cert_renew_loop
  _remove_owned_file_resource tunnel_service_unit
  _remove_owned_file_resource report_service_unit
  _remove_owned_file_resource service_unit
  _remove_owned_file_resource nginx_site_link
  _remove_owned_file_resource nginx_site
  _remove_owned_file_resource caddy_fragment
  _remove_owned_file_resource rmcp_cli
  if command -v systemctl >/dev/null 2>&1; then systemctl daemon-reload >/dev/null 2>&1 || true; fi
  _reload_shared_nginx_after_removal || true

  if [[ "$purge" == true ]]; then
    _remove_owned_certificate_via_certbot || warn 'Owned certificate cleanup through product Certbot failed; namespace removal will still remove product certificate files.'
  fi
  remove_application_payload

  if [[ "$purge" == true ]]; then
    _safe_purge_dir "${LOG_DIR:-}" || true
    _safe_purge_dir "${BACKUP_DIR:-}" || true
    _safe_purge_dir "${SECRET_DIR:-}" || true
    _safe_purge_dir "${CONFIG_DIR:-}" || true
    if [[ "${INSTALL_MODE:-}" == prefix ]]; then
      rmdir "$CODE_BASE" >/dev/null 2>&1 || true
    fi
  else
    mark_uninstalled_state
  fi
}

_stray_path_absent() {
  local label="$1" path="$2" failures_var="$3"
  [[ -z "$path" || ! -e "$path" ]] && return 0
  fail "$label: $path"
  printf -v "$failures_var" '%d' "$(( ${!failures_var} + 1 ))"
}

stray_check() {
  local purge="${1:-false}" failures=0 port="${UNINSTALL_CHECK_PORT:-${RHMCP_LOCAL_PORT:-${LOCAL_PORT:-8765}}}"
  _stray_path_absent 'stray rmcp CLI' "${UNINSTALL_CHECK_CLI:-}" failures
  _stray_path_absent 'stray service unit' "${UNINSTALL_CHECK_UNIT:-}" failures
  _stray_path_absent 'stray tunnel unit' "${UNINSTALL_CHECK_TUNNEL_UNIT:-}" failures
  _stray_path_absent 'stray renewal service' "${UNINSTALL_CHECK_RENEW_SERVICE:-}" failures
  _stray_path_absent 'stray renewal timer' "${UNINSTALL_CHECK_RENEW_TIMER:-}" failures
  _stray_path_absent 'stray portable renewal pidfile' "${UNINSTALL_CHECK_RENEW_PID:-}" failures
  _stray_path_absent 'stray portable renewal start-ticks' "${UNINSTALL_CHECK_RENEW_PID:+${UNINSTALL_CHECK_RENEW_PID}.start_ticks}" failures
  _stray_path_absent 'stray portable renewal loop' "${UNINSTALL_CHECK_RENEW_LOOP:-}" failures
  _stray_path_absent 'stray Nginx site' "${UNINSTALL_CHECK_NGINX_SITE:-}" failures
  _stray_path_absent 'stray Nginx enable link' "${UNINSTALL_CHECK_NGINX_LINK:-}" failures
  [[ -z "${CURRENT_LINK:-}" || ! -e "$CURRENT_LINK" ]] || { fail "stray current link: $CURRENT_LINK"; failures=$((failures+1)); }
  [[ -z "${RELEASES_DIR:-}" || ! -e "$RELEASES_DIR" ]] || { fail "stray releases dir: $RELEASES_DIR"; failures=$((failures+1)); }
  if [[ "$port" =~ ^[0-9]+$ ]] && port_listening "$port"; then
    fail "local port still has a live listener: $port"; failures=$((failures+1))
  fi
  if [[ "$purge" == true ]]; then
    [[ ! -e "${CONFIG_DIR:-/nonexistent}" ]] || { fail "stray config dir: $CONFIG_DIR"; failures=$((failures+1)); }
    [[ -z "${SECRET_DIR:-}" || ! -e "$SECRET_DIR" ]] || { fail "stray secret dir: $SECRET_DIR"; failures=$((failures+1)); }
    [[ -z "${STATE_DIR:-}" || ! -e "$STATE_DIR" ]] || { fail "stray state dir: $STATE_DIR"; failures=$((failures+1)); }
    [[ -z "${RUNTIME_DIR:-}" || ! -e "$RUNTIME_DIR" ]] || { fail "stray runtime dir: $RUNTIME_DIR"; failures=$((failures+1)); }
    if [[ "${INSTALL_MODE:-}" == prefix ]]; then
      [[ ! -e "$CODE_BASE" ]] || { fail "stray prefix root: $CODE_BASE"; failures=$((failures+1)); }
    fi
  fi
  if command -v nginx >/dev/null 2>&1 && pgrep -x nginx >/dev/null 2>&1; then
    nginx -t >/dev/null 2>&1 || { fail 'shared Nginx no longer passes nginx -t'; failures=$((failures+1)); }
  fi
  if (( failures == 0 )); then ok 'Remote Host MCP stray check PASS / 残留检查通过'; return 0; fi
  return 1
}
