#!/usr/bin/env bash
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="${RHMCP_SOURCE_ROOT:-$(cd "$SELF_DIR/.." && pwd)}"
source "$SELF_DIR/lib/i18n.sh"
source "$SELF_DIR/lib/common.sh"
source "$SELF_DIR/lib/ports.sh"
source "$SELF_DIR/lib/paths.sh"
source "$SELF_DIR/lib/state.sh"
source "$SELF_DIR/lib/readiness.sh"
source "$SELF_DIR/lib/lifecycle.sh"
source "$SELF_DIR/lib/external_probe.sh"
source "$SELF_DIR/lib/cloudflare.sh"
source "$SELF_DIR/lib/dns_provider.sh"
source "$SELF_DIR/lib/domain_mode.sh"
source "$SELF_DIR/lib/reverse_proxy.sh"
source "$SELF_DIR/lib/tls.sh"
source "$SELF_DIR/lib/uninstall.sh"

RMCP_VERSION="$(tr -d '\r\n' < "$SOURCE_ROOT/VERSION")"
ACTION='install'
RESUME_MODE=false
RESUME_FROM_STAGE='NONE'
AUTHORITY='user'
INGRESS=''
DOMAIN_DNS_MODE='unknown'
PUBLIC_HOST='mcp.invalid'
PUBLIC_HTTPS_PORT='443'
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
ACME_EMAIL=''
LAST_COMPLETED_STAGE='NONE'
LAST_ERROR_CLASS='INSTALLER_FAILURE'
INSTALL_TRACKING=false
INSTALL_COMPLETE=false
PROMOTED=false
LOCAL_VALIDATED=false

parse_args() {
  while (($#)); do
    case "$1" in
      --resume) ACTION=resume ;;
      --repair) ACTION=repair ;;
      --diagnose) ACTION=diagnose ;;
      --uninstall-incomplete) ACTION=uninstall-incomplete ;;
      --help|-h)
        printf 'Usage: install.sh [--resume|--repair|--diagnose|--uninstall-incomplete]\n'
        exit 0
        ;;
      *) die "Unknown installer argument: $1" ;;
    esac
    shift
  done
}

prime_state_context() {
  [[ -n "${RMCP_INSTALL_STATE:-}" && -f "$RMCP_INSTALL_STATE" ]] || return 0
  # Installer-owned metadata supplied by the installed rmcp launcher.
  # shellcheck disable=SC1090
  source "$RMCP_INSTALL_STATE"
}

on_exit() {
  local rc=$?
  trap - EXIT
  if (( rc != 0 )) && [[ "$INSTALL_TRACKING" == true && "$INSTALL_COMPLETE" != true ]]; then
    set +e
    write_progress_state INCOMPLETE "$LAST_COMPLETED_STAGE" "$LAST_ERROR_CLASS"
    write_install_state INCOMPLETE
    if [[ "$PROMOTED" == true && "$LOCAL_VALIDATED" != true && -n "${PREVIOUS_CURRENT:-}" ]]; then
      rollback_promoted_release
      if [[ "$SERVICE_BACKEND" == systemd ]] && command -v systemctl >/dev/null 2>&1; then systemctl restart remote-host-mcp.service >/dev/null 2>&1 || true; fi
    fi
    warn "Installation is INCOMPLETE at stage ${LAST_COMPLETED_STAGE}. Resume with: rmcp resume (or rerun installer --resume)."
  fi
  exit "$rc"
}
trap on_exit EXIT

stage_rank() {
  case "$1" in
    NONE) printf '0\n' ;;
    PRECHECK) printf '10\n' ;;
    PREPARE) printf '20\n' ;;
    RELEASE) printf '30\n' ;;
    RUNTIME) printf '40\n' ;;
    CLI_RECOVERY) printf '50\n' ;;
    SERVICE) printf '60\n' ;;
    LOCAL_READY) printf '70\n' ;;
    INGRESS) printf '80\n' ;;
    TLS) printf '90\n' ;;
    PUBLIC_READY) printf '100\n' ;;
    MCP_VERIFY) printf '110\n' ;;
    COMPLETE) printf '120\n' ;;
    *) printf '0\n' ;;
  esac
}

resume_has_stage() {
  local target="$1"
  [[ "$RESUME_MODE" == true ]] || return 1
  (( $(stage_rank "$RESUME_FROM_STAGE") >= $(stage_rank "$target") ))
}

stage_done() {
  LAST_COMPLEED_STAGE="$1"
  write_progress_state INCOMPLETE "$LAST_COMPLETED_STAGE" ''
}

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
  command_exists openssl && ok 'openssl' || die 'openssl is required / 需要 openssl'
  python_venv_preflight || die 'Python venv/ensurepip is required. / 需要 Python venv/ensurepip。'
  ok 'Python venv + ensurepip'
  command_exists ssh && ok 'OpenSSH client' || warn 'ssh client not found; ssh_* tools will fail closed until OpenSSH is installed'
  command_exists scp && ok 'SCP client' || warn 'scp client not found; ssh_upload/ssh_download will fail closed until OpenSSH is installed'
  if systemd_operational; then ok 'systemd manager'; else warn 'operational systemd manager unavailable; portable backend will be used where supported'; fi
  [[ -d /mnt/workspace ]] && ok '/mnt/workspace detected' || true
}

preflight_layout() {
  require_disk_space_mb "$CODE_BASE" "${RHMCP_MIN_FREE_MB:-512}" || die 'Insufficient free disk space for staged install (minimum 512 MiB by default).'
  ok 'Disk space preflight'
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
  local choice default_audience url scheme_host
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
      if [[ "$INGRESS" == private ]]; then
        scheme_host="http://127.0.0.1:${LOCAL_PORT}"
      elif [[ "$PUBLIC_HTTPS_PORT" == 443 ]]; then
        scheme_host="https://${PUBLIC_HOST}"
      else
        scheme_host="https://${PUBLIC_HOST}:${PUBLIC_HTTPS_PORT}"
      fi
      default_audience="${scheme_host}/mcp"
      read -r -p "OAuth audience [${default_audience}]: " OAUTH_AUDIENCE
      OAUTH_AUDIENCE="${OAUTH_AUDIENCE:-$default_audience}"
      read -r -p 'OAuth scopes [remote-host]: ' OAUTH_SCOPES
      OAUTH_SCOPES="${OAUTH_SCOPES:-remote-host}"
      for url in "$OAUTH_ISSUER" "$OAUTH_JWKS_URL"; do [[ "$url" == https://* ]] || die 'OAuth issuer/JWKS URLs must use https://'; done
      ;;
    *) die 'Invalid selection / 无效选项' ;;
  esac
}

read_https_port() {
  local input
  read -r -p 'Public HTTPS port [443]: ' input
  PUBLIC_HTTPS_PORT="${input:-443}"
  [[ "$PUBLIC_HTTPS_PORT" =~ ^[0-9]+$ && "$PUBLIC_HTTPS_PORT" -ge 1 && "$PUBLIC_HTTPS_PORT" -le 65535 ]] || die 'Invalid HTTPS port.'
}

choose_ingress() {
  header "$(t ingress)"
  printf '  1. Public IP HTTPS\n  2. Domain HTTPS\n  3. Cloudflare Tunnel\n  4. Private/local only\n'
  local choice host mode
  read -r -p 'Select / 选择 [1-4]: ' choice
  case "$choice" in
    1)
      INGRESS=public-ip
      read -r -p 'Public IPv4 address: ' host
      valid_ipv4 "$host" || die 'Invalid public IPv4 address.'
      PUBLIC_HOST="$host"
      read_https_port
      read -r -p 'ACME contact email: ' ACME_EMAIL
      [[ "$ACME_EMAIL" == *@*.* ]] || die 'Valid ACME contact email is required.'
      ;;
    2)
      INGRESS=domain
      read -r -p "$(t domain_prompt): " host
      host="${host,,}"; host="${host%.}"
      valid_hostname "$host" || die 'Invalid hostname / 域名格式无效'
      PUBLIC_HOST="$host"
      read_https_port
      printf '  1. Cloudflare DNS only\n  2. Cloudflare Proxied (Full strict)\n  3. Other DNS provider / plain DNS\n'
      read -r -p 'DNS mode [1-3]: ' mode
      case "$mode" in 1) DOMAIN_DNS_MODE=cloudflare-dns-only ;; 2) DOMAIN_DNS_MODE=cloudflare-proxied ;; 3) DOMAIN_DNS_MODE=other ;; *) die 'Invalid DNS mode.' ;; esac
      read -r -p 'ACME contact email: ' ACME_EMAIL
      [[ "$ACME_EMAIL" == *@*.* ]] || die 'Valid ACME contact email is required.'
      ;;
    3)
      INGRESS=cloudflare-tunnel
      read -r -p "$(t domain_prompt): " host
      host="${host,,}"; host="${host%.}"
      valid_hostname "$host" || die 'Invalid hostname / 域名格式无效'
      PUBLIC_HOST="$host"; PUBLIC_HTTPS_PORT=443
      ;;
    4)
      INGRESS=private
      PUBLIC_HOST=mcp.invalid; PUBLIC_HTTPS_PORT=443
      ;;
    *) die 'Invalid selection / 无效选项' ;;
  esac
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
RHMCP_BUILD_COMMIT=${RESOLVED_COMMIT}
RHMCP_BUILD_REF=${REQUESTED_REF}
RHMCP_INGRESS_PROFILE=${INGRESS}
RHMCP_PUBLIC_HTTPS_PORT=${PUBLIC_HTTPS_PORT}
RHMCP_DOMAIN_DNS_MODE=${DOMAIN_DNS_MODE}
RHMCP_ACME_EMAIL=${ACME_EMAIL}
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

load_resume_env() {
  local env_file="$CONFIG_DIR/rhmcp.env"
  [[ -f "$env_file" ]] || die 'Resume requires existing rhmcp.env; secrets will not be reconstructed.'
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
  AUTH_MODE="${RHMCP_AUTH_MODE:-$AUTH_MODE}"
  PATH_KEY="${RHMCP_PATH_KEY:-}"
  PUBLIC_HOST="${RHMCP_PUBLIC_HOST:-$PUBLIC_HOST}"
  LOCAL_PORT="${RHMCP_PORT:-$LOCAL_PORT}"
  DOMAIN_DNS_MODE="${RHMCP_DOMAIN_DNS_MODE:-$DOMAIN_DNS_MODE}"
  PUBLIC_HTTPS_PORT="${RHMCP_PUBLIC_HTTPS_PORT:-$PUBLIC_HTTPS_PORT}"
  ACME_EMAIL="${RHMCP_ACME_EMAIL:-$ACME_EMAIL}"
  OAUTH_ISSUER="${RHMCP_OAUTH_ISSUER:-}"
  OAUTH_JWKS_URL="${RHMCP_OAUTH_JWKS_URL:-}"
  OAUTH_AUDIENCE="${RHMCP_OAUTH_AUDIENCE:-}"
  OAUTH_SCOPES="${RHMCP_OAUTH_SCOPES:-remote-host}"
}

copy_release() {
  local release="$RELEASES_DIR/$RELEASE_ID" staging meta_commit
  if [[ -d "$release" ]]; then
    [[ -f "$release/.rhmcp-release.env" ]] || die "Existing release lacks provenance metadata: $release"
    meta_commit="$(awk -F= '$1=="RHMCP_BUILD_COMMIT" {print $2; exit}' "$release/.rhmcp-release.env")"
    [[ "$meta_commit" == "$RESOLVED_COMMIT" ]] || die 'Existing release ID belongs to a different commit.'
    RELEASE_DIR="$release"; export RELEASE_DIR
    return 0
  fi
  staging="${release}.staging.$$"
  rm -rf -- "$staging"
  mkdir -p "$staging"
  (cd "$SOURCE_ROOT" && tar --exclude='.git' --exclude='.venv' --exclude='logs' --exclude='secrets' --exclude='backups' --exclude='.runtime' -cf - .) | (cd "$staging" && tar -xf -)
  write_release_metadata "$staging"
  mv "$staging" "$release"
  ln -sfn "$CONFIG_DIR/rhmcp.env" "$release/.env"
  ln -sfn "$LOG_DIR" "$release/logs"
  ln -sfn "$SECRET_DIR" "$release/secrets"
  ln -sfn "$BACKUP_DIR" "$release/backups"
  ln -sfn "$RUNTIME_DIR" "$release/.runtime"
  RELEASE_DIR="$release"; export RELEASE_DIR
  record_resource release "$release" created
}

install_runtime() {
  if [[ -x "$RELEASE_DIR/.venv/bin/remote-host-mcp" ]]; then ok 'Existing staged runtime is complete; reusing it.'; return 0; fi
  rm -rf -- "$RELEASE_DIR/.venv"
  info 'Creating Python virtual environment / 创建 Python 虚拟环境'
  python3 -m venv "$RELEASE_DIR/.venv"
  "$RELEASE_DIR/.venv/bin/python" -m pip install -q --upgrade pip
  "$RELEASE_DIR/.venv/bin/pip" install -q "$RELEASE_DIR"
  [[ -x "$RELEASE_DIR/.venv/bin/remote-host-mcp" ]] || die 'Runtime install did not create remote-host-mcp entrypoint.'
}

start_service() {
  local rc
  set +e; install_managed_systemd_service; rc=$?; set -e
  case "$rc" in
    0) ;;
    1) start_portable_service_managed ;;
    *) die 'System service path is occupied by a foreign resource; refusing portable fallback that would hide the conflict.' ;;
  esac
}

configure_cloudflare_dns_secret() {
  local token token_file zone
  token_file="$(cloudflare_dns_token_file)"
  if [[ -r "$token_file" ]]; then return 0; fi
  if [[ -n "${RHMCP_CF_DNS_TOKEN_SOURCE:-}" && -r "$RHMCP_CF_DNS_TOKEN_SOURCE" ]]; then
    token="$(tr -d '\r\n' < "$RHMCP_CF_DNS_TOKEN_SOURCE")"
  else
    printf 'Cloudflare DNS API token (Zone DNS Edit + Zone Read, target zone only): '
    read -r -s token; printf '\n'
  fi
  save_cloudflare_dns_token "$token" >/dev/null || { unset token; die 'Invalid Cloudflare DNS token format.'; }
  unset token
  zone="$(_cf_zone_for_name "$PUBLIC_HOST" 2>/dev/null || true)"
  if [[ -z "$zone" ]]; then rm -f "$token_file"; die 'Cloudflare token cannot read the target zone; token removed.'; fi
  ok "Cloudflare DNS token configured at $token_file (secret value not logged)"
}

configure_ingress() {
  local input token_file rc
  case "$INGRESS" in
    private)
      PUBLIC_READY=true
      info 'Private/local profile: no public port is exposed by the installer.'
      ;;
    cloudflare-tunnel)
      token_file="$SECRET_DIR/cloudflared.token"
      if [[ -r "$token_file" ]]; then
        install_managed_tunnel_service "$token_file"
        set +e; external_http_probe "https://${PUBLIC_HOST}/health" 200; rc=$?; set -e
        [[ $rc -eq 0 ]] || die 'Existing Tunnel credential is present but external HTTPS validation failed.'
        PUBLIC_READY=true
      else
        printf '\n%s\n' "$(t tunnel_paste)"
        read -r -p '> ' input
        configure_managed_cloudflare_tunnel "$PUBLIC_HOST" "$input"
        unset input
      fi
      ;;
    domain)
      [[ $EUID -eq 0 ]] || die 'Managed Domain HTTPS requires root/sudo.'
      if [[ "$DOMAIN_DNS_MODE" == cloudflare-proxied ]]; then configure_cloudflare_dns_secret; fi
      configure_domain_https "$PUBLIC_HOST" "$ACME_EMAIL" "$DOMAIN_DNS_MODE" "$PUBLIC_HTTPS_PORT"
      PUBLIC_READY=true
      ;;
    public-ip)
      [[ $EUID -eq 0 ]] || die 'Managed Public IP HTTPS requires root/sudo.'
      configure_public_ip_https "$PUBLIC_HOST" "$ACME_EMAIL" "$PUBLIC_HTTPS_PORT"
      PUBLIC_READY=true
      ;;
    *) die "Unknown ingress profile: $INGRESS" ;;
  esac
}

public_health_reusable() {
  local url rc
  [[ "$INGRESS" != private ]] || return 0
  if [[ "$PUBLIC_HTTPS_PORT" == 443 ]]; then url="https://${PUBLIC_HOST}/health"; else url="https://${PUBLIC_HOST}:${PUBLIC_HTTPS_PORT}/health"; fi
  set +e; external_http_probe "$url" 200; rc=$?; set -e
  [[ $rc -eq 0 ]]
}

mcp_final_validation() {
  local base url bearer="${RHMCP_VALIDATION_BEARER_TOKEN:-}"
  if [[ "$INGRESS" == private ]]; then
    base="http://127.0.0.1:${LOCAL_PORT}"
  elif [[ "$PUBLIC_HTTPS_PORT" == 443 ]]; then
    base="https://${PUBLIC_HOST}"
  else
    base="https://${PUBLIC_HOST}:${PUBLIC_HTTPS_PORT}"
  fi
  if [[ "$AUTH_MODE" == capability ]]; then
    [[ -n "$PATH_KEY" ]] || die 'Capability path key is missing.'
    url="${base}/mcp/${PATH_KEY}"
  else
    url="${base}/mcp"
    if [[ -z "$bearer" ]]; then
      printf 'OAuth validation bearer token (used once; not stored): '
      read -r -s bearer; printf '\n'
    fi
    [[ -n "$bearer" ]] || die 'OAuth final MCP validation requires a one-time bearer token.'
  fi
  RHMCP_VALIDATE_URL="$url" RHMCP_VALIDATION_BEARER_TOKEN="$bearer" RHMCP_VALIDATE_TOOL_COUNT=65 \
    "$CURRENT_LINK/.venv/bin/python" "$CURRENT_LINK/installer/validate_mcp.py"
  unset bearer url
}

show_result() {
  header "$(t done)"
  printf 'Version / 版本        : %s\nBuild commit / 提交   : %s\nBuild ref / 引用      : %s\nInstall root / 路径   : %s\nAuthority / 权限      : %s\nIngress / 接入        : %s\nLocal endpoint / 本地 : http://127.0.0.1:%s\n' "$RMCP_VERSION" "$RESOLVED_COMMIT" "$REQUESTED_REF" "$CODE_BASE" "$AUTHORITY" "$INGRESS" "$LOCAL_PORT"
  if [[ "$INGRESS" != private ]]; then printf 'Public endpoint / 公网 : https://%s%s\n' "$PUBLIC_HOST" "$([[ "$PUBLIC_HTTPS_PORT" == 443 ]] && printf '' || printf ':%s' "$PUBLIC_HTTPS_PORT")"; fi
  if [[ "$AUTH_MODE" == capability ]]; then
    subhr
    if [[ "$INGRESS" == private ]]; then
      printf '%s:\n\nhttp://127.0.0.1:%s/mcp/%s\n\n' "$(t copy_url)" "$LOCAL_PORT" "$PATH_KEY"
    else
      printf '%s:\n\nhttps://%s%s/mcp/%s\n\n' "$(t copy_url)" "$PUBLIC_HOST" "$([[ "$PUBLIC_HTTPS_PORT" == 443 ]] && printf '' || printf ':%s' "$PUBLIC_HTTPS_PORT")" "$PATH_KEY"
    fi
    warn "$(t secret_warn)"
  else
    printf 'Authentication / 认证 : OAuth 2.1\n'
  fi
  hr; printf 'Management / 管理: rmcp status | rmcp doctor | rmcp uninstall --dry-run\n'
}

handle_existing_transaction() {
  local source_commit choice
  source_commit="$RESOLVED_COMMIT"
  if [[ -f "$INSTALL_STATE" ]]; then
    load_install_state "$INSTALL_STATE" || true
    if [[ "${RHMCP_INSTALL_STATUS:-}" == COMPLETE && "$ACTION" == install ]]; then
      die 'A complete installation already exists. Use rmcp status/doctor/repair or uninstall before reinstalling.'
    fi
  fi
  if [[ ! -f "$PROGRESS_STATE" ]]; then
    [[ "$ACTION" != resume ]] || die 'No incomplete transaction exists to resume.'
    return 0
  fi
  load_progress_state || return 0
  [[ "${INSTALL_STATUS:-}" == INCOMPLETE ]] || return 0
  if [[ "$ACTION" == install ]]; then
    warn "Found incomplete install at stage ${LAST_COMPLETED_STAGE:-unknown}."
    printf '  1. Resume\n  2. Diagnose\n  3. Ownership-aware purge of incomplete install\n  0. Exit\n'
    read -r -p 'Select [0-3]: ' choice
    case "$choice" in 1) ACTION=resume ;; 2) ACTION=diagnose ;; 3) ACTION=uninstall-incomplete ;; *) exit 0 ;; esac
  fi
  case "$ACTION" in
    resume)
      [[ "${TARGET_COMMIT:-}" == "$source_commit" ]] || die "Resume requires original commit ${TARGET_COMMIT:-unknown}; current source is $source_commit."
      RESUME_FROM_STAGE="${LAST_COMPLETED_STAGE:-NONE}"
      restore_transaction_state || die 'Incomplete transaction metadata is not resumable.'
      RESUME_MODE=true
      load_resume_env
      ;;
    diagnose)
      if [[ -f "$INSTALL_STATE" ]]; then load_install_layout_from_state "$INSTALL_STATE"; fi
      diagnose_install
      exit 0
      ;;
    uninstall-incomplete)
      restore_transaction_state || true
      LOCAL_PORT="${TARGET_LOCAL_PORT:-$LOCAL_PORT}"
      if resource_owned certificate && [[ "${RHMCP_PURGE_CERTIFICATES:-0}" != 1 ]]; then
        warn 'Incomplete install owns an ACME certificate. Review the purge plan before confirming removal.'
      fi
      uninstall_plan true
      confirm 'Apply purge of product-owned incomplete resources?' || exit 0
      RHMCP_PURGE_CERTIFICATES=1 uninstall_apply true
      stray_check true
      exit 0
      ;;
  esac
}

run_repair_or_diagnose_from_state() {
  [[ -n "${RMCP_INSTALL_STATE:-}" && -f "$RMCP_INSTALL_STATE" ]] || return 1
  load_install_layout_from_state "$RMCP_INSTALL_STATE"
  load_locale "${RHMCP_LANGUAGE:-en_US}"
  LOCAL_PORT="${RHMCP_LOCAL_PORT:-$LOCAL_PORT}"
  case "$ACTION" in repair) repair_local_lifecycle ;; diagnose) diagnose_install ;; *) return 1 ;; esac
  exit 0
}

prepare_action_layout() {
  if [[ "$ACTION" == resume && -n "${RMCP_INSTALL_STATE:-}" && -f "$RMCP_INSTALL_STATE" ]]; then
    load_install_layout_from_state "$RMCP_INSTALL_STATE" || die 'Install state contains an unsafe or invalid layout.'
  else
    choose_layout
  fi
  preflight_layout
  resolve_build_provenance
  handle_existing_transaction
}

main() {
  parse_args "$@"
  prime_state_context
  if [[ "$ACTION" == repair || "$ACTION" == diagnose ]]; then run_repair_or_diagnose_from_state || true; fi
  select_language
  header "$(t title) $RMCP_VERSION"
  preflight
  prepare_action_layout

  if [[ "$ACTION" == repair || "$ACTION" == diagnose ]]; then
    [[ -f "$INSTALL_STATE" ]] || die 'No install-state found for repair/diagnose.'
    load_install_layout_from_state "$INSTALL_STATE"
    if [[ "$ACTION" == repair ]]; then repair_local_lifecycle; else diagnose_install; fi
    return 0
  fi

  if [[ "$RESUME_MODE" != true ]]; then
    choose_authority
    choose_port 8765; LOCAL_PORT="$CHOSEN_PORT"
    choose_ingress
    choose_auth
  fi

  prepare_layout_dirs
  INSTALL_TRACKING=true
  if [[ "$RESUME_MODE" != true ]]; then
    write_progress_state INCOMPLETE PRECHECK ''
    write_install_state INCOMPLETE
    LAST_COMPLETED_STAGE=PRECHECK
    write_env
    stage_done PREPARE
  else
    LAST_COMPLETED_STAGE="$RESUME_FROM_STAGE"
    info "Resuming fixed commit ${RESOLVED_COMMIT} from completed stage ${RESUME_FROM_STAGE}."
  fi

  copy_release
  if ! resume_has_stage RELEASE; then stage_done RELEASE; fi
  install_runtime
  if ! resume_has_stage RUNTIME; then stage_done RUNTIME; fi

  promote_release; PROMOTED=true
  write_install_state INCOMPLETE
  install_rmcp_launcher
  if ! resume_has_stage CLI_RECOVERY; then stage_done CLI_RECOVERY; fi

  if resume_has_stage SERVICE && wait_local_health "$LOCAL_PORT" 2 1; then
    info 'Existing managed service is already healthy; reusing it.'
  else
    start_service
    stage_done SERVICE
  fi

  if ! wait_local_health "$LOCAL_PORT" "${RHMCP_READINESS_TIMEOUT_S:-30}" 1; then
    LAST_ERROR_CLASS=READINESS_TIMEOUT
    service_readiness_diagnostics "$LOCAL_PORT"
    return 1
  fi
  LOCAL_VALIDATED=true
  if ! resume_has_stage LOCAL_READY; then stage_done LOCAL_READY; fi

  if resume_has_stage PUBLIC_READY && public_health_reusable; then
    PUBLIC_READY=true
    info 'Existing public/TLS layer revalidated; skipping duplicate ingress/certificate creation.'
  else
    configure_ingress
    stage_done INGRESS
    stage_done TLS
    [[ "$PUBLIC_READY" == true ]] || { LAST_ERROR_CLASS=PUBLIC_NOT_READY; return 1; }
    stage_done PUBLIC_READY
  fi

  mcp_final_validation
  stage_done MCP_VERIFY

  write_install_state COMPLETE
  write_progress_state COMPLETE COMPLETE ''
  LAST_COMPLETED_STAGE=COMPLETE
  INSTALL_COMPLETE=true
  show_result
}

main "$@"
