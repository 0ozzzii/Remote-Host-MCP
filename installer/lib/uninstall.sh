#!/usr/bin/env bash
set -euo pipefail

_uninstall_print() { printf '  %-8s %s\n' "$1" "$2"; }

uninstall_plan() {
  local purge="${1:-false}" cli unit tunnel_unit nginx_site cert_name
  cli="$(resource_value rmcp_cli PATH 2>/dev/null || true)"
  unit="$(resource_value service_unit PATH 2>/dev/null || true)"
  tunnel_unit="$(resource_value tunnel_service_unit PATH 2>/dev/null || true)"
  nginx_site="$(resource_value nginx_site PATH 2>/dev/null || true)"
  cert_name="$(resource_value certificate PATH 2>/dev/null || true)"
  header 'Remote Host MCP uninstall plan / 卸载计划'
  _uninstall_print REMOVE "application releases/current: ${CODE_BASE:-unknown}"
  _uninstall_print REMOVE "runtime state: ${STATE_DIR:-unknown}"
  [[ -n "$cli" ]] && _uninstall_print REMOVE "owned rmcp CLI: $cli"
  [[ -n "$unit" ]] && _uninstall_print REMOVE "owned service unit: $unit"
  [[ -n "$tunnel_unit" ]] && _uninstall_print REMOVE "owned tunnel unit: $tunnel_unit"
  [[ -n "$nginx_site" ]] && _uninstall_print REMOVE "owned ingress file: $nginx_site"
  _uninstall_print KEEP "system nginx/caddy/apache, certbot, python and unrelated services"
  _uninstall_print KEEP "reused/shared resources and unrelated certificates/DNS records"
  if [[ "$purge" == true ]]; then
    _uninstall_print REMOVE "owned product config/secrets/hooks: ${CONFIG_DIR:-unknown}"
    _uninstall_print REMOVE "owned logs/backups: ${LOG_DIR:-unknown} / ${BACKUP_DIR:-unknown}"
    if [[ -n "$cert_name" ]] && resource_owned certificate; then
      if [[ "${RHMCP_PURGE_CERTIFICATES:-0}" == 1 ]]; then
        _uninstall_print REMOVE "owned certificate (explicit opt-in): $cert_name"
      else
        _uninstall_print KEEP "owned certificate (requires RHMCP_PURGE_CERTIFICATES=1): $cert_name"
      fi
    fi
  else
    _uninstall_print KEEP "config/secrets/backups for recovery; use --purge for owned namespace removal"
  fi
}

_stop_owned_services() {
  local unit tunnel_unit
  unit="$(resource_value service_unit PATH 2>/dev/null || true)"
  tunnel_unit="$(resource_value tunnel_service_unit PATH 2>/dev/null || true)"
  if [[ -n "$tunnel_unit" ]] && resource_owned tunnel_service_unit && command -v systemctl >/dev/null 2>&1; then
    systemctl disable --now "$(basename "$tunnel_unit")" >/dev/null 2>&1 || true
  fi
  if [[ -n "$unit" ]] && resource_owned service_unit && command -v systemctl >/dev/null 2>&1; then
    systemctl disable --now "$(basename "$unit")" >/dev/null 2>&1 || true
  elif [[ "${RHMCP_SERVICE_BACKEND:-}" == portable && -x "${CURRENT_LINK:-}/scripts/manage.sh" ]]; then
    (cd "$CURRENT_LINK" && bash scripts/manage.sh stop >/dev/null 2>&1) || true
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

_remove_owned_certificate() {
  local cert
  [[ "${RHMCP_PURGE_CERTIFICATES:-0}" == 1 ]] || return 0
  resource_owned certificate || return 0
  cert="$(resource_value certificate PATH 2>/dev/null || true)"
  [[ -n "$cert" && "$cert" != */* ]] || return 1
  command -v certbot >/dev/null 2>&1 || return 0
  certbot delete --cert-name "$cert" --non-interactive >/dev/null 2>&1 || return 1
}

uninstall_apply() {
  local purge="${1:-false}"
  [[ -n "${CODE_BASE:-}" && "$CODE_BASE" = /* ]] || die 'Unsafe or missing CODE_BASE; refusing uninstall.'
  [[ -n "${CONFIG_DIR:-}" && "$CONFIG_DIR" = /* ]] || die 'Unsafe or missing CONFIG_DIR; refusing uninstall.'
  uninstall_plan "$purge"
  _stop_owned_services
  _remove_owned_file_resource tunnel_service_unit
  _remove_owned_file_resource service_unit
  _remove_owned_file_resource nginx_site
  _remove_owned_file_resource caddy_fragment
  _remove_owned_file_resource rmcp_cli
  if command -v systemctl >/dev/null 2>&1; then systemctl daemon-reload >/dev/null 2>&1 || true; fi
  if [[ -d "$CODE_BASE" ]]; then rm -rf -- "$CODE_BASE"; fi
  if [[ -n "${STATE_DIR:-}" && "$STATE_DIR" = /* && -d "$STATE_DIR" ]]; then rm -rf -- "$STATE_DIR"; fi
  if [[ "$purge" == true ]]; then
    _remove_owned_certificate || warn 'Owned certificate cleanup failed; leaving certificate intact for manual review.'
    [[ -n "${LOG_DIR:-}" && "$LOG_DIR" = /* && -d "$LOG_DIR" ]] && rm -rf -- "$LOG_DIR"
    [[ -n "${RUNTIME_DIR:-}" && "$RUNTIME_DIR" = /* && -d "$RUNTIME_DIR" ]] && rm -rf -- "$RUNTIME_DIR"
    [[ -n "${BACKUP_DIR:-}" && "$BACKUP_DIR" = /* && -d "$BACKUP_DIR" ]] && rm -rf -- "$BACKUP_DIR"
    # CONFIG_DIR is last because it contains the ownership registry used above.
    [[ -d "$CONFIG_DIR" ]] && rm -rf -- "$CONFIG_DIR"
  else
    if [[ -f "${INSTALL_STATE:-}" ]]; then
      printf 'RHMCP_INSTALL_STATUS=UNINSTALLED\n' > "${INSTALL_STATE}.uninstalled"
      chmod 600 "${INSTALL_STATE}.uninstalled" 2>/dev/null || true
    fi
  fi
}

stray_check() {
  local port="${RHMCP_LOCAL_PORT:-${LOCAL_PORT:-8765}}" failures=0 cli unit tunnel_unit nginx_site
  cli="$(resource_value rmcp_cli PATH 2>/dev/null || true)"
  unit="$(resource_value service_unit PATH 2>/dev/null || true)"
  tunnel_unit="$(resource_value tunnel_service_unit PATH 2>/dev/null || true)"
  nginx_site="$(resource_value nginx_site PATH 2>/dev/null || true)"
  [[ ! -e "${CODE_BASE:-/nonexistent}" ]] || { fail "stray code base: $CODE_BASE"; failures=$((failures+1)); }
  [[ -z "$cli" || ! -e "$cli" ]] || { fail "stray rmcp CLI: $cli"; failures=$((failures+1)); }
  [[ -z "$unit" || ! -e "$unit" ]] || { fail "stray service unit: $unit"; failures=$((failures+1)); }
  [[ -z "$tunnel_unit" || ! -e "$tunnel_unit" ]] || { fail "stray tunnel unit: $tunnel_unit"; failures=$((failures+1)); }
  [[ -z "$nginx_site" || ! -e "$nginx_site" ]] || { fail "stray ingress file: $nginx_site"; failures=$((failures+1)); }
  if [[ "$port" =~ ^[0-9]+$ ]] && ! port_free "$port"; then
    fail "local port still listening: $port"
    failures=$((failures+1))
  fi
  if [[ "${1:-false}" == true ]]; then
    [[ ! -e "${CONFIG_DIR:-/nonexistent}" ]] || { fail "stray config dir: $CONFIG_DIR"; failures=$((failures+1)); }
  fi
  if (( failures == 0 )); then ok 'Remote Host MCP stray check PASS / 残留检查通过'; return 0; fi
  return 1
}
