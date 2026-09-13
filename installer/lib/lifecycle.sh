#!/usr/bin/env bash
set -euo pipefail

managed_file_has_marker() {
  local path="$1"
  [[ -f "$path" ]] && grep -q '^# Managed-By: remote-host-mcp$' "$path" 2>/dev/null
}

atomic_symlink() {
  local target="$1" link="$2" tmp="${link}.tmp.$$"
  rm -f -- "$tmp"
  ln -s "$target" "$tmp"
  mv -Tf -- "$tmp" "$link"
}

promote_release() {
  [[ -n "${RELEASE_DIR:-}" && -d "$RELEASE_DIR" ]] || die 'Release directory is missing; refusing promotion.'
  PREVIOUS_CURRENT=''
  if [[ -L "$CURRENT_LINK" ]]; then PREVIOUS_CURRENT="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"; fi
  atomic_symlink "$RELEASE_DIR" "$CURRENT_LINK"
  record_resource current_link "$CURRENT_LINK" created
  export PREVIOUS_CURRENT
}

rollback_promoted_release() {
  [[ -n "${PREVIOUS_CURRENT:-}" && -d "$PREVIOUS_CURRENT" ]] || return 0
  atomic_symlink "$PREVIOUS_CURRENT" "$CURRENT_LINK"
  warn "Restored previous current release: $PREVIOUS_CURRENT"
}

install_managed_systemd_service() {
  local unit='/etc/systemd/system/remote-host-mcp.service' tmp
  [[ "$INSTALL_MODE" == system && $EUID -eq 0 ]] || return 1
  systemd_operational || return 1
  if [[ -e "$unit" ]] && ! managed_file_has_marker "$unit"; then
    fail "Foreign systemd unit already exists: $unit"
    return 2
  fi
  tmp="${unit}.tmp.$$"
  cat > "$tmp" <<EOF2
# Managed-By: remote-host-mcp
[Unit]
Description=Remote Host MCP
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
EnvironmentFile=${CONFIG_DIR}/rhmcp.env
WorkingDirectory=${CURRENT_LINK}
ExecStart=${CURRENT_LINK}/.venv/bin/remote-host-mcp
Restart=on-failure
RestartSec=2
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
EOF2
  chmod 644 "$tmp"
  mv -f "$tmp" "$unit"
  record_resource service_unit "$unit" created
  if [[ "$AUTHORITY" == user ]]; then chown -R "$SERVICE_USER":"$(id -gn "$SERVICE_USER")" "$STATE_DIR" "$LOG_DIR" "$RUNTIME_DIR"; fi
  systemctl daemon-reload
  systemctl enable --now remote-host-mcp.service
  SERVICE_BACKEND=systemd
  export SERVICE_BACKEND
}

start_portable_service_managed() {
  (cd "$CURRENT_LINK" && bash scripts/manage.sh restart >/dev/null)
  SERVICE_BACKEND=portable
  record_resource portable_server_pid "$LOG_DIR/server.pid" created
  export SERVICE_BACKEND
}

install_rmcp_launcher() {
  local wrapper release_root target tmp
  release_root="${RELEASE_DIR:-$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)}"
  [[ -n "$release_root" && -f "$release_root/scripts/rmcp.sh" ]] || die 'Cannot install rmcp launcher: controller release is unavailable.'
  target="$release_root/scripts/rmcp.sh"
  if [[ $EUID -eq 0 && -d /usr/local/bin ]]; then
    wrapper=/usr/local/bin/rmcp
  else
    mkdir -p "${HOME}/.local/bin"
    wrapper="${HOME}/.local/bin/rmcp"
  fi
  if [[ -e "$wrapper" ]] && ! managed_file_has_marker "$wrapper"; then
    die "Refusing to overwrite foreign rmcp command: $wrapper"
  fi
  tmp="${wrapper}.tmp.$$"
  cat > "$tmp" <<EOF2
#!/usr/bin/env bash
# Managed-By: remote-host-mcp
export RMCP_INSTALL_STATE='${INSTALL_STATE}'
export RMCP_CONTROLLER_ROOT='${release_root}'
exec bash '${target}' "\$@"
EOF2
  chmod 755 "$tmp"
  mv -f "$tmp" "$wrapper"
  record_resource rmcp_cli "$wrapper" created
  record_resource rmcp_controller "$target" created
  ok "rmcp -> $wrapper"
}

upsert_env_value() {
  local file="$1" key="$2" value="$3" tmp="${file}.tmp.$$"
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || return 2
  if [[ -f "$file" ]]; then
    awk -v k="$key" -v v="$value" 'BEGIN{d=0} $0 ~ "^" k "=" {print k "=" v; d=1; next} {print} END{if(!d) print k "=" v}' "$file" > "$tmp"
  else
    printf '%s=%s\n' "$key" "$value" > "$tmp"
  fi
  chmod 600 "$tmp"
  mv "$tmp" "$file"
}

restore_provenance_from_release() {
  local meta="$CURRENT_LINK/.rhmcp-release.env" commit ref
  [[ -f "$meta" ]] || return 1
  # shellcheck disable=SC1090
  source "$meta"
  commit="${RHMCP_BUILD_COMMIT:-}"
  ref="${RHMCP_BUILD_REF:-}"
  [[ "$commit" =~ ^[0-9a-fA-F]{40}$ && -n "$ref" ]] || return 1
  upsert_env_value "$CONFIG_DIR/rhmcp.env" RHMCP_BUILD_COMMIT "${commit,,}"
  upsert_env_value "$CONFIG_DIR/rhmcp.env" RHMCP_BUILD_REF "$ref"
}

_safe_install_path() {
  local path="$1"
  [[ -n "$path" && "$path" = /* && "$path" != / ]]
}

load_install_layout_from_state() {
  local state_file="$1" _p
  load_install_state "$state_file" || return 1
  INSTALL_MODE="${RHMCP_INSTALL_MODE:-system}"
  CODE_BASE="${RHMCP_CODE_BASE:-}"
  CONFIG_DIR="${RHMCP_CONFIG_DIR:-}"
  STATE_DIR="${RHMCP_STATE_DIR_PERSIST:-}"
  LOG_DIR="${RHMCP_LOG_DIR:-}"
  SECRET_DIR="${RHMCP_SECRET_DIR_PERSIST:-}"
  RUNTIME_DIR="${RHMCP_RUNTIME_DIR_PERSIST:-}"
  BACKUP_DIR="${RHMCP_BACKUP_DIR:-${STATE_DIR}/backups}"
  for _p in "$CODE_BASE" "$CONFIG_DIR" "$STATE_DIR" "$LOG_DIR" "$SECRET_DIR" "$RUNTIME_DIR" "$BACKUP_DIR"; do _safe_install_path "$_p" || return 2; done
  RELEASES_DIR="${CODE_BASE}/releases"
  CURRENT_LINK="${CODE_BASE}/current"
  INSTALL_STATE="$state_file"
  PROGRESS_STATE="${RHMCP_PROGRESS_STATE:-${CONFIG_DIR}/install-progress.env}"
  OWNERSHIP_STATE="${RHMCP_OWNERSHIP_STATE:-${CONFIG_DIR}/resource-ownership.env}"
  CERTBOT_DIR="${CONFIG_DIR}/certbot"
  CERTBOT_SECRET_DIR="${SECRET_DIR}/dns"
  AUTHORITY="${RHMCP_AUTHORITY:-user}"
  INGRESS="${RHMCP_INGRESS:-private}"
  LOCAL_PORT="${RHMCP_LOCAL_PORT:-8765}"
  PUBLIC_HOST="${RHMCP_PUBLIC_HOST_STATE:-mcp.invalid}"
  PUBLIC_HTTPS_PORT="${RHMCP_PUBLIC_HTTPS_PORT:-443}"
  SERVICE_BACKEND="${RHMCP_SERVICE_BACKEND:-portable}"
  export INSTALL_MODE CODE_BASE CONFIG_DIR STATE_DIR LOG_DIR SECRET_DIR RUNTIME_DIR BACKUP_DIR RELEASES_DIR CURRENT_LINK INSTALL_STATE PROGRESS_STATE OWNERSHIP_STATE CERTBOT_DIR CERTBOT_SECRET_DIR AUTHORITY INGRESS LOCAL_PORT PUBLIC_HOST PUBLIC_HTTPS_PORT SERVICE_BACKEND
}

diagnose_install() {
  printf 'Install state      : %s\n' "${RHMCP_INSTALL_STATUS:-unknown}"
  printf 'Version            : %s\n' "${RHMCP_INSTALL_VERSION:-${RMCP_VERSION:-unknown}}"
  printf 'Build commit       : %s\n' "${RHMCP_BUILD_COMMIT:-unknown}"
  printf 'Build ref          : %s\n' "${RHMCP_BUILD_REF:-unknown}"
  printf 'Ingress            : %s\n' "${RHMCP_INGRESS:-${INGRESS:-unknown}}"
  printf 'Local endpoint     : http://127.0.0.1:%s\n' "${RHMCP_LOCAL_PORT:-${LOCAL_PORT:-8765}}"
  printf 'Current release    : %s\n' "$(readlink -f "${CURRENT_LINK:-/nonexistent}" 2>/dev/null || printf 'missing')"
  if [[ -f "${PROGRESS_STATE:-}" ]]; then
    # shellcheck disable=SC1090
    source "$PROGRESS_STATE"
    printf 'Transaction status : %s\n' "${INSTALL_STATUS:-unknown}"
    printf 'Last stage         : %s\n' "${LAST_COMPLETED_STAGE:-unknown}"
    printf 'Last error class   : %s\n' "${LAST_ERROR_CLASS:-none}"
  fi
  if [[ "${SERVICE_BACKEND:-}" == systemd ]] && command -v systemctl >/dev/null 2>&1; then
    printf 'Service active     : %s\n' "$(systemctl is-active remote-host-mcp.service 2>/dev/null || true)"
    printf 'Service enabled    : %s\n' "$(systemctl is-enabled remote-host-mcp.service 2>/dev/null || true)"
  fi
  if wait_local_health "${RHMCP_LOCAL_PORT:-${LOCAL_PORT:-8765}}" 2 1; then printf 'Local health       : PASS\n'; else printf 'Local health       : FAIL\n'; fi
}

repair_local_lifecycle() {
  local env_file="$CONFIG_DIR/rhmcp.env"
  [[ -d "$CURRENT_LINK" || -L "$CURRENT_LINK" ]] || die 'Current release is missing; use installer resume or reinstall.'
  [[ -f "$env_file" ]] || die 'Runtime environment is missing; automatic secret reconstruction is intentionally refused.'
  restore_provenance_from_release || warn 'Could not repair build provenance from release metadata.'
  install_rmcp_launcher
  if [[ "$SERVICE_BACKEND" == systemd ]]; then
    if ! managed_file_has_marker /etc/systemd/system/remote-host-mcp.service; then
      die 'Systemd unit is missing or not product-managed; refusing to overwrite a foreign unit.'
    fi
    systemctl daemon-reload
    systemctl enable remote-host-mcp.service >/dev/null
    if ! wait_local_health "$LOCAL_PORT" 2 1; then systemctl restart remote-host-mcp.service; fi
  else
    if ! wait_local_health "$LOCAL_PORT" 2 1; then start_portable_service_managed; fi
  fi
  if ! wait_local_health "$LOCAL_PORT" 30 1; then service_readiness_diagnostics "$LOCAL_PORT"; return 1; fi
  ok 'Local lifecycle repair PASS / 本地生命周期修复通过'
}
