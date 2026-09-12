#!/usr/bin/env bash
set -euo pipefail

resolve_root() {
  if [[ -n "${RMCP_INSTALL_STATE:-}" && -f "$RMCP_INSTALL_STATE" ]]; then
    source "$RMCP_INSTALL_STATE"
    printf '%s/current\n' "$RHMCP_CODE_BASE"
    return
  fi
  cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd
}
ROOT="$(resolve_root)"
export RMCP_ROOT="$ROOT"
LIB_ROOT="$ROOT/installer"
source "$LIB_ROOT/lib/i18n.sh"
source "$LIB_ROOT/lib/common.sh"
source "$LIB_ROOT/lib/update.sh"
source "$ROOT/scripts/lib.sh"
cd "$ROOT"
ensure_dirs

VERSION="$(tr -d '\r\n' < "$ROOT/VERSION" 2>/dev/null || printf 'unknown')"
STATE_FILE="${RMCP_INSTALL_STATE:-}"
if [[ -n "$STATE_FILE" && -f "$STATE_FILE" ]]; then
  source "$STATE_FILE"
  load_locale "${RHMCP_LANGUAGE:-en_US}"
else
  load_locale "${RHMCP_LANGUAGE:-en_US}"
fi

case "${1:-}" in
  --root) printf '%s\n' "$ROOT"; exit 0 ;;
  --version|-V) printf 'Remote Host MCP %s\n' "$VERSION"; exit 0 ;;
  --help|-h) printf 'Usage: rmcp [--root|--version|--help]\n'; exit 0 ;;
esac

pause_menu() { printf '\n'; read -r -p "$(t press_enter)" _ || true; }

service_status() {
  if [[ "${RHMCP_SERVICE_BACKEND:-portable}" == systemd ]] && command -v systemctl >/dev/null 2>&1; then
    systemctl is-active remote-host-mcp.service 2>/dev/null || true
  else
    bash scripts/manage.sh status 2>/dev/null | head -1 || true
  fi
}

service_action() {
  local action="$1"
  if [[ "${RHMCP_SERVICE_BACKEND:-portable}" == systemd ]] && command -v systemctl >/dev/null 2>&1; then
    systemctl "$action" remote-host-mcp.service
  else
    case "$action" in start|stop|restart) bash scripts/manage.sh "$action" ;; *) return 2 ;; esac
  fi
}

show_connection() { print_final_connection; }

rotate_key() {
  load_env || die 'Missing configuration / 缺少配置'
  warn 'Rotating the key invalidates the current MCP URL immediately. / 重置密钥会立即使旧 URL 失效。'
  confirm || return 0
  local backup new
  backup="$(backup_file .env env-before-key-rotation)"
  new="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"
  set_mcp_env_value PATH_KEY "$new"
  if service_action restart >/dev/null && bash scripts/manage.sh check >/dev/null 2>&1; then
    ok 'Key rotated / 密钥已重置'
    show_connection
  else
    restore_file "$backup" .env
    service_action restart >/dev/null 2>&1 || true
    warn 'Restart/health failed; previous key restored. / 验证失败，已恢复旧密钥。'
  fi
}

check_update() {
  local remote
  remote="$(latest_version || true)"
  [[ -n "$remote" ]] || { warn 'Could not query update channel / 无法查询更新通道'; return; }
  printf 'Current / 当前: %s\nLatest / 最新: %s\n' "$VERSION" "$remote"
  if version_is_newer "$VERSION" "$remote"; then warn "$(t update_available): $remote"; else ok "$(t update_none)"; fi
}

change_language() {
  local choice lang
  printf '1. 简体中文\n2. English\n'
  read -r -p 'Select / 选择 [1-2]: ' choice
  case "$choice" in 1) lang=zh_CN ;; 2) lang=en_US ;; *) return ;; esac
  load_locale "$lang"
  if [[ -n "$STATE_FILE" && -f "$STATE_FILE" ]]; then
    python3 - "$STATE_FILE" "$lang" <<'PY'
from pathlib import Path
import re, sys
p=Path(sys.argv[1]); lang=sys.argv[2]
s=p.read_text()
line=f"RHMCP_LANGUAGE={lang}"
if re.search(r'^RHMCP_LANGUAGE=.*$', s, flags=re.M):
    s=re.sub(r'^RHMCP_LANGUAGE=.*$', line, s, flags=re.M)
else:
    s += ('\n' if s and not s.endswith('\n') else '') + line + '\n'
p.write_text(s)
PY
  fi
}

while true; do
  clear 2>/dev/null || true
  header "Remote Host MCP $VERSION"
  printf 'Status / 状态 : %s\n' "$(service_status | head -1)"
  printf 'Root / 路径   : %s\n' "$ROOT"
  printf 'Ingress / 接入: %s\n' "${RHMCP_INGRESS:-legacy/source}"
  subhr
  if [[ "$RMCP_LANGUAGE" == zh_CN ]]; then
    cat <<'MENU'
  1. 查看连接信息
  2. 启动服务
  3. 停止服务
  4. 重启服务
  5. 重置连接密钥
  6. 完整诊断
  7. 查看服务日志
  8. 检查更新
  9. 修改语言
  0. 退出
MENU
  else
    cat <<'MENU'
  1. Show connection information
  2. Start service
  3. Stop service
  4. Restart service
  5. Rotate connection key
  6. Full diagnostics
  7. View service logs
  8. Check for updates
  9. Change language
  0. Exit
MENU
  fi
  subhr
  read -r -p 'Select / 选择 [0-9]: ' choice
  case "$choice" in
    1) show_connection; pause_menu ;;
    2) service_action start; pause_menu ;;
    3) service_action stop; pause_menu ;;
    4) service_action restart; pause_menu ;;
    5) rotate_key; pause_menu ;;
    6) bash scripts/manage.sh check || true; pause_menu ;;
    7)
      if [[ "${RHMCP_SERVICE_BACKEND:-portable}" == systemd ]] && command -v journalctl >/dev/null 2>&1; then
        journalctl -u remote-host-mcp.service -n 120 --no-pager
      else
        bash scripts/manage.sh logs 120
      fi
      pause_menu
      ;;
    8) check_update; pause_menu ;;
    9) change_language ;;
    0) exit 0 ;;
    *) warn 'Invalid selection / 无效选项'; sleep 1 ;;
  esac
done
