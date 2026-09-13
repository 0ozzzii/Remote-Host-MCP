#!/usr/bin/env bash
set -euo pipefail

_config_valid_port() {
  local port="$1"
  [[ "$port" =~ ^[0-9]+$ && "$port" -ge 1 && "$port" -le 65535 ]]
}

_config_env_get() {
  local key="$1" file="$CONFIG_DIR/rhmcp.env"
  [[ -f "$file" ]] || return 1
  awk -F= -v k="$key" '$1==k {sub(/^[^=]*=/, ""); print; exit}' "$file"
}

_config_state_upsert() {
  local key="$1" value="$2" file="$INSTALL_STATE" tmp
  [[ -f "$file" ]] || return 1
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || return 2
  tmp="${file}.tmp.$$"
  awk -v k="$key" -v v="$value" '
    BEGIN { done=0 }
    $0 ~ "^" k "=" { print k "=" v; done=1; next }
    { print }
    END { if (!done) print k "=" v }
  ' "$file" > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$file"
}

_config_protocol_gate() {
  local public_port="$1" host auth key bearer base url rc
  host="$(_config_env_get RHMCP_PUBLIC_HOST 2>/dev/null || true)"
  auth="$(_config_env_get RHMCP_AUTH_MODE 2>/dev/null || printf 'capability')"
  bearer="${RHMCP_VALIDATION_BEARER_TOKEN:-}"
  [[ -n "$host" && "$host" != mcp.invalid ]] || return 1
  if [[ "$public_port" == 443 ]]; then base="https://${host}"; else base="https://${host}:${public_port}"; fi
  if [[ "$auth" == capability ]]; then
    key="$(_config_env_get RHMCP_PATH_KEY 2>/dev/null || true)"
    [[ -n "$key" ]] || return 1
    url="${base}/mcp/${key}"
  else
    if [[ -z "$bearer" ]]; then
      warn 'OAuth protocol gate skipped for this configuration change: set RHMCP_VALIDATION_BEARER_TOKEN for a full MCP gate.'
      return 0
    fi
    url="${base}/mcp"
  fi
  set +e
  RHMCP_VALIDATE_URL="$url" RHMCP_VALIDATION_BEARER_TOKEN="$bearer" RHMCP_VALIDATE_TOOL_COUNT=65 \
    "$CURRENT_LINK/.venv/bin/python" "$CURRENT_LINK/installer/validate_mcp.py" >/dev/null
  rc=$?
  set -e
  return "$rc"
}

_config_restore_snapshot() {
  local env_snapshot="$1" state_snapshot="$2" nginx_snapshot="$3" nginx_config="$4"
  cp -p "$env_snapshot" "$CONFIG_DIR/rhmcp.env"
  cp -p "$state_snapshot" "$INSTALL_STATE"
  if [[ -n "$nginx_snapshot" && -f "$nginx_snapshot" ]]; then
    cp -p "$nginx_snapshot" "$nginx_config"
    reload_nginx_safely >/dev/null 2>&1 || true
  fi
}

apply_https_port_mapping() {
  local new_public="$1" new_listen="$2" env_file="$CONFIG_DIR/rhmcp.env"
  local host backend_port cert_name cert_fullchain cert_privkey cert_mode nginx_config
  local old_public old_listen http_listener env_snapshot state_snapshot nginx_snapshot='' rc=0

  _config_valid_port "$new_public" || { fail "Invalid public HTTPS port: $new_public"; return 2; }
  _config_valid_port "$new_listen" || { fail "Invalid local HTTPS listen port: $new_listen"; return 2; }
  case "${INGRESS:-private}" in
    domain|public-ip) ;;
    *) fail 'HTTPS port mapping is only applicable to managed Domain/Public-IP ingress.'; return 2 ;;
  esac
  [[ $EUID -eq 0 ]] || { fail 'Managed HTTPS reconfiguration requires root/sudo.'; return 2; }
  [[ -f "$env_file" && -f "$INSTALL_STATE" ]] || { fail 'Installation configuration/state is missing.'; return 2; }

  old_public="$(_config_env_get RHMCP_PUBLIC_HTTPS_PORT 2>/dev/null || printf '443')"
  old_listen="$(_config_env_get RHMCP_HTTPS_LISTEN_PORT 2>/dev/null || printf '%s' "$old_public")"
  if [[ "$new_public" == "$old_public" && "$new_listen" == "$old_listen" ]]; then
    info 'HTTPS port mapping unchanged.'
    return 0
  fi

  host="$(_config_env_get RHMCP_PUBLIC_HOST 2>/dev/null || true)"
  backend_port="$(_config_env_get RHMCP_PORT 2>/dev/null || printf '8765')"
  cert_name="$(_config_env_get RHMCP_CERT_NAME 2>/dev/null || true)"
  cert_fullchain="$(_config_env_get RHMCP_CERT_FULLCHAIN 2>/dev/null || true)"
  cert_mode="$(_config_env_get RHMCP_CERT_RENEWAL_MODE 2>/dev/null || true)"
  cert_privkey="${CONFIG_DIR}/letsencrypt/live/${cert_name}/privkey.pem"
  [[ -n "$host" && -n "$cert_name" && -r "$cert_fullchain" && -r "$cert_privkey" ]] || {
    fail 'Existing managed certificate material is incomplete; run rmcp repair before changing HTTPS ports.'
    return 2
  }
  nginx_config="$(nginx_product_config_path 2>/dev/null || true)"
  [[ -n "$nginx_config" && -f "$nginx_config" ]] || {
    fail 'Managed Nginx config is unavailable; refusing configuration rewrite.'
    return 2
  }
  managed_file_has_marker "$nginx_config" || {
    fail 'Nginx product path is foreign; refusing configuration rewrite.'
    return 2
  }

  if [[ "$new_listen" != "$old_listen" ]]; then
    bind_port_free_any "$new_listen" || { fail "Local HTTPS listen port is occupied: $new_listen"; return 2; }
  fi
  if [[ "${INGRESS:-}" == public-ip || "$cert_mode" == http-01-* ]]; then http_listener=true; else http_listener=false; fi

  env_snapshot="$(mktemp "$STATE_DIR/configure-env.XXXXXX")"
  state_snapshot="$(mktemp "$STATE_DIR/configure-state.XXXXXX")"
  nginx_snapshot="$(mktemp "$STATE_DIR/configure-nginx.XXXXXX")"
  chmod 600 "$env_snapshot" "$state_snapshot" "$nginx_snapshot"
  cp -p "$env_file" "$env_snapshot"
  cp -p "$INSTALL_STATE" "$state_snapshot"
  cp -p "$nginx_config" "$nginx_snapshot"

  upsert_env_value "$env_file" RHMCP_PUBLIC_HTTPS_PORT "$new_public"
  upsert_env_value "$env_file" RHMCP_HTTPS_LISTEN_PORT "$new_listen"
  _config_state_upsert RHMCP_PUBLIC_HTTPS_PORT "$new_public"
  _config_state_upsert RHMCP_HTTPS_LISTEN_PORT "$new_listen"

  set +e
  write_managed_nginx_https "$host" "$backend_port" "$STATE_DIR/acme-webroot" "$cert_fullchain" "$cert_privkey" "$new_listen" "$new_public" "$http_listener"
  rc=$?
  if [[ $rc -eq 0 ]]; then verify_external_https "$host" "$new_public"; rc=$?; fi
  if [[ $rc -eq 0 ]]; then _config_protocol_gate "$new_public"; rc=$?; fi
  set -e

  if [[ $rc -ne 0 ]]; then
    _config_restore_snapshot "$env_snapshot" "$state_snapshot" "$nginx_snapshot" "$nginx_config"
    rm -f "$env_snapshot" "$state_snapshot" "$nginx_snapshot"
    fail 'HTTPS port change failed validation and was rolled back.'
    return 1
  fi

  rm -f "$env_snapshot" "$state_snapshot" "$nginx_snapshot"
  PUBLIC_HTTPS_PORT="$new_public"
  HTTPS_LISTEN_PORT="$new_listen"
  export PUBLIC_HTTPS_PORT HTTPS_LISTEN_PORT
  ok "HTTPS port mapping applied: public=${new_public} -> local-listen=${new_listen}"
}

configure_network_menu() {
  local choice current_public current_listen input new_public new_listen
  while true; do
    current_public="$(_config_env_get RHMCP_PUBLIC_HTTPS_PORT 2>/dev/null || printf '443')"
    current_listen="$(_config_env_get RHMCP_HTTPS_LISTEN_PORT 2>/dev/null || printf '%s' "$current_public")"
    header 'Remote Host MCP configuration / 配置'
    printf 'Current public HTTPS port / 公网端口 : %s\n' "$current_public"
    printf 'Current local listen port / 本机监听 : %s\n' "$current_listen"
    printf '\n  1. Change public HTTPS port / 修改公网 HTTPS 端口\n'
    printf '  2. Change local HTTPS listen port / 修改本机 HTTPS 监听端口\n'
    printf '  3. Change public + local mapping / 同时修改公网→本机端口映射\n'
    printf '  0. Back / 返回\n\n'
    read -r -p 'Select / 选择 [0-3]: ' choice
    case "$choice" in
      1)
        read -r -p "Public HTTPS port [${current_public}]: " input
        new_public="${input:-$current_public}"
        apply_https_port_mapping "$new_public" "$current_listen" || true
        ;;
      2)
        read -r -p "Local HTTPS listen port [${current_listen}]: " input
        new_listen="${input:-$current_listen}"
        apply_https_port_mapping "$current_public" "$new_listen" || true
        ;;
      3)
        read -r -p "Public HTTPS port [${current_public}]: " input
        new_public="${input:-$current_public}"
        read -r -p "Local HTTPS listen port [${current_listen}] (usually same; change only for provider/NAT mapping): " input
        new_listen="${input:-$current_listen}"
        apply_https_port_mapping "$new_public" "$new_listen" || true
        ;;
      0) return 0 ;;
      *) warn 'Invalid selection / 无效选项' ;;
    esac
    printf '\n'; read -r -p 'Press Enter / 回车继续...' _ || true
  done
}
