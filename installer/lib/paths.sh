#!/usr/bin/env bash
set -euo pipefail

is_dsw_like() {
  [[ -d /mnt/workspace ]] || [[ "${PWD:-}" == /mnt/workspace* ]]
}

choose_layout() {
  local choice base default_base
  header "$(t install_mode)"
  if is_dsw_like; then
    default_base='/mnt/workspace/remote-host-mcp'
    info 'Detected /mnt/workspace; persistent-prefix mode is recommended. / 检测到 /mnt/workspace，推荐持久化目录安装。'
  else
    default_base="${HOME:-/tmp}/remote-host-mcp"
  fi
  printf '  1. %s\n' "$(t mode_standard)"
  printf '  2. %s\n' "$(t mode_persistent)"
  printf '  3. %s\n' "$(t mode_custom)"
  read -r -p 'Select / 选择 [1-3]: ' choice
  case "$choice" in
    1)
      [[ $EUID -eq 0 ]] || die 'Standard system install requires root/sudo. / 标准系统安装需要 root/sudo。'
      INSTALL_MODE=system
      CODE_BASE=/opt/remote-host-mcp
      CONFIG_DIR=/etc/remote-host-mcp
      STATE_DIR=/var/lib/remote-host-mcp
      LOG_DIR=/var/log/remote-host-mcp
      SECRET_DIR=/etc/remote-host-mcp/secrets
      BACKUP_DIR=/var/lib/remote-host-mcp/backups
      RUNTIME_DIR=/var/lib/remote-host-mcp/runtime
      ;;
    2)
      INSTALL_MODE=prefix
      read -r -p "Install root / 安装根目录 [${default_base}]: " base
      base="${base:-$default_base}"
      [[ "$base" = /* ]] || die 'Install root must be absolute. / 安装根目录必须是绝对路径。'
      CODE_BASE="$base"
      CONFIG_DIR="$base/config"
      STATE_DIR="$base/state"
      LOG_DIR="$base/logs"
      SECRET_DIR="$base/secrets"
      BACKUP_DIR="$base/backups"
      RUNTIME_DIR="$base/runtime"
      ;;
    3)
      INSTALL_MODE=prefix
      read -r -p 'Absolute install root / 绝对安装根目录: ' base
      [[ "$base" = /* ]] || die 'Install root must be absolute. / 安装根目录必须是绝对路径。'
      CODE_BASE="$base"
      CONFIG_DIR="$base/config"
      STATE_DIR="$base/state"
      LOG_DIR="$base/logs"
      SECRET_DIR="$base/secrets"
      BACKUP_DIR="$base/backups"
      RUNTIME_DIR="$base/runtime"
      ;;
    *) die 'Invalid selection / 无效选项' ;;
  esac
  RELEASES_DIR="$CODE_BASE/releases"
  CURRENT_LINK="$CODE_BASE/current"
  INSTALL_STATE="$CONFIG_DIR/install-state.env"
  export INSTALL_MODE CODE_BASE CONFIG_DIR STATE_DIR LOG_DIR SECRET_DIR BACKUP_DIR RUNTIME_DIR RELEASES_DIR CURRENT_LINK INSTALL_STATE
}

prepare_layout_dirs() {
  install -d "$CODE_BASE" "$RELEASES_DIR" "$CONFIG_DIR" "$STATE_DIR" "$LOG_DIR" "$RUNTIME_DIR"
  install -d -m 700 "$SECRET_DIR" "$BACKUP_DIR"
}
