#!/usr/bin/env bash
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="${RHMCP_SOURCE_ROOT:-$(cd "$SELF_DIR/.." && pwd)}"
source "$SELF_DIR/lib/i18n.sh"
source "$SELF_DIR/lib/common.sh"
source "$SELF_DIR/lib/ports.sh"
source "$SELF_DIR/lib/paths.sh"
source "$SELF_DIR/lib/state.sh"
source "$SELF_DIR/lib/cloudflare.sh"
source "$SELF_DIR/lib/reverse_proxy.sh"

RMCP_VERSION="$(tr -d '\r\n' < "$SOURCE_ROOT/VERSION")"
AUTHORITY='user'
INGRESS=''
PUBLIC_HOST='mcp.invalid'
LOCAL_PORT='8765'
SERVICE_BACKEND='portable'
SERVICE_USER="$(id -un)"
PUBLIC_READY=false
AUTH_MODE='capability'
PATH_KEY=''
OAUTH_ISSUER=''
OAUTH_JWKS_URL=''
OAUTH_AUDIENCE=''
OAUTH_SCOPES='remote-host'

select_language() {
  if [[ -n "${RHMCP_LANGUAGE:-}" ]]; then load_locale "$RHMCP_LANGUAGE"; return; fi
  clear 2>/dev/null || true
  hr
  printf ' Remote Host MCP Installer %s\n' "$RMCP_VERSION"
  hr
  printf ' 请选择安装语言 / Select language\n\n'
  printf '  1. 简体中文\n  2. English\n\n'
  local choice
  read -r -p '请选择 / Select [1-2]: ' choice
  case "$choice" in 1) load_locale zh_CN ;; 2|*) load_locale en_US ;; esac
}

preflight() {
  header "$(t preflight)"
  require_python
  ok "Python $(python3 --version 2>&1 | awk '{print $2}')"
  command_exists curl && ok 'curl' || die 'curl is required / 需要 curl'
  command_exists tar && ok 'tar' || die 'tar is required / 需要 tar'
  command_exists ssh && ok 'OpenSSH client' || warn 'ssh client not found; ssh_* tools will fail closed until OpenSSH is installed'
  command_exists scp && ok 'SCP client' || warn 'scp client not found; ssh_upload/ssh_download will fail closed until OpenSSH is installed'
  if command_exists systemctl && [[ -d /run/systemd/system ]]; then ok 'systemd'; else warn 'systemd unavailable; portable service backend will be used'; fi
  [[ -d /mnt/workspace ]] && ok '/mnt/workspace detected' || true
}

choose_authority() {
  header "$(t authority)"
  printf '  1. %s\n  2. %s\n' "$(t authority_root)" "$(t authority_user)"
  local choice
  read -r -p 'Select / 选择 [1-2]: ' choice
  case "$choice" in
    1)
      warn "$(t authority_warning)"
      confirm || die 'Cancelled / 已取消'
      [[ $EUID -eq 0 ]] || die 'Full host control requires root/sudo. / 完整主机控制需要 root/sudo。'
      AUTHORITY=root; SERVICE_USER=root
      ;;
    2)
      AUTHORITY=user
      if [[ $EUID -eq 0 ]]; then
        SERVICE_USER="${SUDO_USER:-}"
        if [[ -z "$SERVICE_USER" || "$SERVICE_USER" == root ]]; then read -r -p 'Linux service user / Linux 运行用户: ' SERVICE_USER; fi
        id "$SERVICE_USER" >/dev/null 2>&1 || die 'Unknown Linux user / Linux 用户不存在'
      else SERVICE_USER="$(id -un)"; fi
      ;;
    *) die 'Invalid selection / 无效选项' ;;
  esac
}

choose_auth() {
  header "$(t auth)"
  printf '  1. %s\n  2. %s\n' "$(t auth_capability)" "$(t auth_oauth)"
  local choice default_audience url
  read -r -p 'Select / 选择 [1-2]: ' choice
  case "$choice" in
    1)
      AUTH_MODE=capability
      PATH_KEY="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"
      ;;
    2)
      AUTH_MODE=oauth
      PATH_KEY=''
      read -r -p 'OAuth issuer (https://...) / OAuth Issuer: ' OAUTH_ISSUER
      read -r -p 'JWKS URL (https://...) / JWKS 地址: ' OAUTH_JWKS_URL
      default_audience="https://${PUBLIC_HOST}/mcp"
      read -r -p "OAuth audience [${default_audience}]: " OAUTH_AUDIENCE
      OAUTH_AUDIENCE="${OAUTH_AUDIENCE:-$default_audience}"
      read -r -p 'OAuth scopes [remote-host]: ' OAUTH_SCOPES
      OAUTH_SCOPES="${OAUTH_SCOPES:-remote-host}"
      for url in "$OAUTH_ISSUER" "$OAUTH_JWKS_URL" "$OAUTH_AUDIENCE"; do
        [[ "$url" == https://* ]] || die 'OAuth URLs must use https:// / OAuth URL 必须使用 https://'
      done
      ;;
    *) die 'Invalid selection / 无效选项' ;;
  esac
}

choose_ingress() {
  header "$(t ingress)"
  printf '  1. %s\n  2. %s\n' "$(t ingress_direct)" "$(t ingress_tunnel)"
  local choice host
  read -r -p 'Select / 选择 [1-2]: ' choice
  case "$choice" in 1) INGRESS=direct ;; 2) INGRESS=cloudflare-tunnel ;; *) die 'Invalid selection / 无效选项' ;; esac
  read -r -p "$(t domain_prompt): " host
  host="${host,,}"; host="${host%.}"
  valid_hostname "$host" || die 'Invalid hostname / 域名格式无效'
  PUBLIC_HOST="$host"
}

write_env() {
  local env_file="$CONFIG_DIR/rhmcp.env" roots user_home
  if [[ "$AUTHORITY" == root ]]; then
    roots='/'
  else
    user_home="$(getent passwd "$SERVICE_USER" 2>/dev/null | cut -d: -f6)"
    user_home="${user_home:-${HOME:-/tmp}}"
    roots="${user_home}:/tmp"
  fi
  umask 077
  cat > "$env_file" <<EOF2
RHMCP_AUTH_MODE=${AUTH_MODE}
RHMCP_PATH_KEY=${PATH_KEY}
RHMCP_PUBLIC_HOST=${PUBLIC_HOST}
RHMCP_BIND_HOST=127.0.0.1
RHMCP_PORT=${LOCAL_PORT}
RHMCP_ALLOWED_ROOTS=${roots}
RHMCP_STATE_DIR=${STATE_DIR}
RHMCP_DEFAULT_TIMEOUT_MS=30000
RHMCP_MAX_TIMEOUT_MS=90000
RHMCP_JSON_RESPONSE=true
RHMCP_STATELESS_HTTP=true
RHMCP_TASKS_EXTENSION=true
EOF2
  if [[ "$AUTH_MODE" == oauth ]]; then
    cat >> "$env_file" <<EOF2
RHMCP_OAUTH_ISSUER=${OAUTH_ISSUER}
RHMCP_OAUTH_JWKS_URL=${OAUTH_JWKS_URL}
RHMCP_OAUTH_AUDIENCE=${OAUTH_AUDIENCE}
RHMCP_OAUTH_SCOPES=${OAUTH_SCOPES}
RHMCP_OAUTH_ALGORITHMS=RS256
EOF2
  fi
  chmod 600 "$env_file"
}

copy_release() {
  local short release
  short="$(git -C "$SOURCE_ROOT" rev-parse --short HEAD 2>/dev/null || printf 'source')"
  release="$RELEASES_DIR/${RMCP_VERSION}-${short}"
  [[ ! -e "$release" ]] || release="$RELEASES_DIR/${RMCP_VERSION}-${short}-$(date +%Y%m%d%H%M%S)"
  mkdir -p "$release"
  (cd "$SOURCE_ROOT" && tar --exclude='.git' --exclude='.venv' --exclude='logs' --exclude='secrets' --exclude='backups' --exclude='.runtime' -cf - .) | (cd "$release" && tar -xf -)
  ln -sfn "$CONFIG_DIR/rhmcp.env" "$release/.env"
  ln -sfn "$LOG_DIR" "$release/logs"
  ln -sfn "$SECRET_DIR" "$release/secrets"
  ln -sfn "$BACKUP_DIR" "$release/backups"
  ln -sfn "$RUNTIME_DIR" "$release/.runtime"
  ln -sfn "$release" "$CURRENT_LINK"
  RELEASE_DIR="$release"; export RELEASE_DIR
}

install_runtime() {
  info 'Creating Python virtual environment / 创建 Python 虚拟环境'
  python3 -m venv "$RELEASE_DIR/.venv"
  "$RELEASE_DIR/.venv/bin/python" -m pip install -q --upgrade pip
  "$RELEASE_DIR/.venv/bin/pip" install -q "$RELEASE_DIR"
}

install_systemd_service() {
  [[ "$INSTALL_MODE" == system && $EUID -eq 0 && -d /run/systemd/system ]] || return 1
  cat > /etc/systemd/system/remote-host-mcp.service <<EOF2
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
  if [[ "$AUTHORITY" == user ]]; then chown -R "$SERVICE_USER":"$(id -gn "$SERVICE_USER")" "$STATE_DIR" "$LOG_DIR" "$RUNTIME_DIR"; fi
  systemctl daemon-reload
  systemctl enable --now remote-host-mcp.service
  SERVICE_BACKEND=systemd
}

start_portable_service() { (cd "$CURRENT_LINK" && bash scripts/manage.sh restart >/dev/null); SERVICE_BACKEND=portable; }
local_health() { curl -fsS --max-time 8 "http://127.0.0.1:${LOCAL_PORT}/health" >/dev/null; }

configure_tunnel() {
  local input token token_file cf
  printf '\n%s\n' "$(t tunnel_paste)"
  read -r -p '> ' input
  token="$(extract_cloudflare_token "$input")" || die 'Could not parse a valid Tunnel Token. / 未识别到有效 Tunnel Token。'
  token_file="$(save_cloudflare_token "$token" "$SECRET_DIR")"; unset token input
  ok "Tunnel Token saved / Tunnel Token 已安全保存: $token_file"
  warn "Cloudflare Published Application must route ${PUBLIC_HOST} to http://127.0.0.1:${LOCAL_PORT}"
  if [[ "$SERVICE_BACKEND" == systemd ]]; then
    cf="$(ensure_cloudflared_binary "$RUNTIME_DIR")"
    cat > /etc/systemd/system/remote-host-mcp-tunnel.service <<EOF2
[Unit]
Description=Remote Host MCP Cloudflare Tunnel
After=network-online.target remote-host-mcp.service
Wants=network-online.target
Requires=remote-host-mcp.service

[Service]
Type=simple
ExecStart=${cf} tunnel --no-autoupdate run --token-file ${token_file}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF2
    systemctl daemon-reload; systemctl enable --now remote-host-mcp-tunnel.service
  else
    CFD_HOST="$PUBLIC_HOST" bash "$CURRENT_LINK/scripts/setup-cft.sh" --reuse-token || true
  fi
  for _ in {1..12}; do
    if curl -fsS --max-time 8 "https://${PUBLIC_HOST}/health" >/dev/null 2>&1; then PUBLIC_READY=true; break; fi
    sleep 2
  done
  [[ "$PUBLIC_READY" == true ]] || warn 'Tunnel is configured but public health is not ready yet. / Tunnel 已配置，但公网健康检查尚未通过。'
}

configure_direct() {
  prepare_direct_ingress "$PUBLIC_HOST" "$LOCAL_PORT"
  if curl -fsS --max-time 8 "https://${PUBLIC_HOST}/health" >/dev/null 2>&1; then PUBLIC_READY=true
  elif [[ "$DIRECT_READY" != true ]]; then warn 'Direct HTTPS requires the generated reverse-proxy/TLS configuration to be activated before ChatGPT can connect.'; fi
}

register_rmcp() {
  local wrapper target
  target="$CURRENT_LINK/scripts/rmcp.sh"
  if [[ $EUID -eq 0 && -d /usr/local/bin ]]; then wrapper=/usr/local/bin/rmcp; else mkdir -p "${HOME}/.local/bin"; wrapper="${HOME}/.local/bin/rmcp"; fi
  cat > "$wrapper" <<EOF2
#!/usr/bin/env bash
export RMCP_INSTALL_STATE='${INSTALL_STATE}'
exec bash '${target}' "\$@"
EOF2
  chmod 755 "$wrapper"
  ok "rmcp -> $wrapper"
}

show_result() {
  header "$(t done)"
  printf 'Version / 版本        : %s\nInstall root / 路径   : %s\nAuthority / 权限      : %s\nIngress / 接入        : %s\nLocal endpoint / 本地 : http://127.0.0.1:%s\nPublic host / 域名    : %s\n' "$RMCP_VERSION" "$CODE_BASE" "$AUTHORITY" "$INGRESS" "$LOCAL_PORT" "$PUBLIC_HOST"
  if [[ "$PUBLIC_READY" == true ]]; then
    if [[ "$AUTH_MODE" == capability ]]; then
      subhr; printf '%s:\n\nhttps://%s/mcp/%s\n\n' "$(t copy_url)" "$PUBLIC_HOST" "$PATH_KEY"; warn "$(t secret_warn)"
    else printf 'MCP URL               : https://%s/mcp\n' "$PUBLIC_HOST"; fi
  else
    warn 'Public endpoint is not verified yet; the secret URL is intentionally not printed. / 公网入口尚未验证，暂不显示完整私密 URL。'
  fi
  hr; printf 'Management / 管理: rmcp\n'
}

main() {
  select_language
  header "$(t title) $RMCP_VERSION"
  preflight
  choose_layout
  choose_authority
  choose_port 8765; LOCAL_PORT="$CHOSEN_PORT"
  choose_ingress
  choose_auth
  prepare_layout_dirs
  write_env
  copy_release
  install_runtime
  if ! install_systemd_service; then start_portable_service; fi
  local_health || die 'Local health check failed / 本地健康检查失败'
  case "$INGRESS" in cloudflare-tunnel) configure_tunnel ;; direct) configure_direct ;; esac
  write_install_state
  register_rmcp
  show_result
}

main "$@"
