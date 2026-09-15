#!/usr/bin/env bash
set -euo pipefail

CERTBOT_BIN=''
ACME_CONFIG_DIR=''
ACME_WORK_DIR=''
ACME_LOG_DIR=''
ACME_WEBROOT=''
CERT_NAME=''
CERT_FULLCHAIN=''
CERT_PRIVKEY=''
CERT_RENEWAL_MODE=''

version_ge() {
  python3 - "$1" "$2" <<'PY'
import re,sys
def v(s):
    p=[int(x) for x in re.findall(r'\d+',s)[:3]]
    return tuple((p+[0,0,0])[:3])
raise SystemExit(0 if v(sys.argv[1]) >= v(sys.argv[2]) else 1)
PY
}

bind_port_free_any() {
  local port="$1"
  python3 - "$port" <<'PY' >/dev/null 2>&1
import socket,sys
p=int(sys.argv[1])
s=socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.bind(('0.0.0.0', p))
finally:
    s.close()
PY
}

install_nginx_dependency() {
  [[ $EUID -eq 0 ]] || die 'Direct HTTPS on ports 80/443 requires root/sudo. / 80/443 直连 HTTPS 需要 root/sudo。'
  if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y nginx
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y nginx
  elif command -v yum >/dev/null 2>&1; then
    yum install -y nginx
  else
    die 'No supported package manager found for Nginx auto-install. / 未找到可自动安装 Nginx 的包管理器。'
  fi
}

ensure_nginx_for_direct() {
  local proxy installed=false
  proxy="$(detect_proxy)"
  case "$proxy" in
    nginx) record_resource host_nginx "$(command -v nginx)" shared; return 0 ;;
    caddy|apache)
      die "Active ${proxy} detected. Automatic direct-HTTPS takeover is refused; use Cloudflare Tunnel/private mode or integrate manually."
      ;;
  esac

  bind_port_free_any 80 || die 'TCP port 80 is already occupied by a non-Nginx listener; refusing managed HTTP-01 takeover.'
  if [[ "${PUBLIC_HTTPS_PORT:-443}" != 80 ]]; then
    bind_port_free_any "${PUBLIC_HTTPS_PORT:-443}" || die "TCP port ${PUBLIC_HTTPS_PORT:-443} is already occupied by a non-Nginx listener; refusing managed HTTPS takeover."
  fi

  if ! command -v nginx >/dev/null 2>&1; then
    if [[ "${RHMCP_AUTO_INSTALL_DEPS:-0}" == 1 ]] || confirm 'Nginx is required for managed HTTPS. Install it now?'; then
      install_nginx_dependency
    else
      die 'Nginx is required for managed direct HTTPS.'
    fi
    installed=true
  fi
  if [[ "$installed" == true ]]; then record_resource host_nginx "$(command -v nginx)" created; else record_resource host_nginx "$(command -v nginx)" shared; fi
  if systemd_operational; then
    systemctl enable --now nginx
  elif ! pgrep -x nginx >/dev/null 2>&1; then
    nginx
  fi
}

ensure_product_certbot() {
  local venv="$RUNTIME_DIR/certbot-venv" version=''
  install -d -m 700 "$RUNTIME_DIR"
  if [[ -x "$venv/bin/certbot" ]]; then version="$($venv/bin/certbot --version 2>/dev/null | awk '{print $2}' || true)"; fi
  if [[ -z "$version" ]] || ! version_ge "$version" 5.4; then
    rm -rf -- "$venv"
    python3 -m venv "$venv"
    "$venv/bin/python" -m pip install -q --upgrade pip
    "$venv/bin/python" -m pip install -q 'certbot>=5.4'
    version="$($venv/bin/certbot --version | awk '{print $2}')"
  fi
  version_ge "$version" 5.4 || die 'Certbot 5.4+ is required for the supported lifecycle.'
  CERTBOT_BIN="$venv/bin/certbot"
  ACME_CONFIG_DIR="$CONFIG_DIR/letsencrypt"
  ACME_WORK_DIR="$STATE_DIR/certbot-work"
  ACME_LOG_DIR="$LOG_DIR/certbot"
  ACME_WEBROOT="$STATE_DIR/acme-webroot"
  install -d -m 700 "$ACME_CONFIG_DIR" "$ACME_WORK_DIR" "$ACME_LOG_DIR"
  install -d -m 755 "$ACME_WEBROOT/.well-known/acme-challenge"
  record_resource certbot_runtime "$venv" created
  record_resource acme_config "$ACME_CONFIG_DIR" created
  record_resource acme_webroot "$ACME_WEBROOT" created
  export CERTBOT_BIN ACME_CONFIG_DIR ACME_WORK_DIR ACME_LOG_DIR ACME_WEBROOT
}

certbot_run() {
  "$CERTBOT_BIN" --config-dir "$ACME_CONFIG_DIR" --work-dir "$ACME_WORK_DIR" --logs-dir "$ACME_LOG_DIR" "$@"
}

prepare_certbot_hooks() {
  local provider="${1:-cloudflare}" dns_lib auth cleanup reload renew token_file
  install -d -m 700 "$CERTBOT_DIR"
  dns_lib="$CERTBOT_DIR/dns_provider.sh"
  cp "$CURRENT_LINK/installer/lib/dns_provider.sh" "$dns_lib"
  chmod 700 "$dns_lib"
  token_file="$(cloudflare_dns_token_file)"
  auth="$CERTBOT_DIR/cloudflare-auth.sh"
  cleanup="$CERTBOT_DIR/cloudflare-cleanup.sh"
  reload="$CERTBOT_DIR/reload-nginx.sh"
  renew="$CERTBOT_DIR/renew.sh"
  cat > "$auth" <<EOF2
#!/usr/bin/env bash
set -euo pipefail
CERTBOT_SECRET_DIR=$(printf '%q' "$CERTBOT_SECRET_DIR")
RUNTIME_DIR=$(printf '%q' "$STATE_DIR/certbot-api")
RHMCP_CF_DNS_TOKEN_FILE=$(printf '%q' "$token_file")
RHMCP_DNS_PROVIDER=$(printf '%q' "$provider")
source $(printf '%q' "$dns_lib")
certbot_dns_hook auth
EOF2
  cat > "$cleanup" <<EOF2
#!/usr/bin/env bash
set -euo pipefail
CERTBOT_SECRET_DIR=$(printf '%q' "$CERTBOT_SECRET_DIR")
RUNTIME_DIR=$(printf '%q' "$STATE_DIR/certbot-api")
RHMCP_CF_DNS_TOKEN_FILE=$(printf '%q' "$token_file")
RHMCP_DNS_PROVIDER=$(printf '%q' "$provider")
source $(printf '%q' "$dns_lib")
certbot_dns_hook cleanup
EOF2
  cat > "$reload" <<'EOF2'
#!/usr/bin/env bash
set -euo pipefail
nginx -t
if command -v systemctl >/dev/null 2>&1 && systemctl is-active nginx >/dev/null 2>&1; then systemctl reload nginx; else nginx -s reload; fi
EOF2
  cat > "$renew" <<EOF2
#!/usr/bin/env bash
set -euo pipefail
exec $(printf '%q' "$CERTBOT_BIN") --config-dir $(printf '%q' "$ACME_CONFIG_DIR") --work-dir $(printf '%q' "$ACME_WORK_DIR") --logs-dir $(printf '%q' "$ACME_LOG_DIR") renew --quiet --deploy-hook $(printf '%q' "$reload")
EOF2
  chmod 700 "$auth" "$cleanup" "$reload" "$renew"
  record_resource certbot_dns_lib "$dns_lib" created
  record_resource certbot_auth_hook "$auth" created
  record_resource certbot_cleanup_hook "$cleanup" created
  record_resource certbot_reload_hook "$reload" created
  record_resource certbot_renew_hook "$renew" created
  CERTBOT_AUTH_HOOK="$auth"; CERTBOT_CLEANUP_HOOK="$cleanup"; CERTBOT_RELOAD_HOOK="$reload"; CERTBOT_RENEW_HOOK="$renew"
  export CERTBOT_AUTH_HOOK CERTBOT_CLEANUP_HOOK CERTBOT_RELOAD_HOOK CERTBOT_RENEW_HOOK
}

setup_renewal_timer() {
  local service='/etc/systemd/system/remote-host-mcp-cert-renew.service' timer='/etc/systemd/system/remote-host-mcp-cert-renew.timer' tmp
  [[ $EUID -eq 0 ]] || die 'Managed certificate renewal requires root.'
  systemd_operational || die 'Managed public HTTPS requires an operational systemd manager for renewal.'
  for tmp in "$service" "$timer"; do
    if [[ -e "$tmp" ]] && ! managed_file_has_marker "$tmp"; then die "Foreign systemd resource exists: $tmp"; fi
  done
  cat > "${service}.tmp.$$" <<EOF2
# Managed-By: remote-host-mcp
[Unit]
Description=Renew Remote Host MCP certificate
After=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/bash ${CERTBOT_DIR}/renew.sh
EOF2
  chmod 644 "${service}.tmp.$$"; mv -f "${service}.tmp.$$" "$service"
  cat > "${timer}.tmp.$$" <<'EOF2'
# Managed-By: remote-host-mcp
[Unit]
Description=Renew Remote Host MCP certificate twice daily

[Timer]
OnCalendar=*-*-* 00,12:00:00
Persistent=true
RandomizedDelaySec=900

[Install]
WantedBy=timers.target
EOF2
  chmod 644 "${timer}.tmp.$$"; mv -f "${timer}.tmp.$$" "$timer"
  record_resource cert_renew_service "$service" created
  record_resource cert_renew_timer "$timer" created
  systemctl daemon-reload
  systemctl enable --now remote-host-mcp-cert-renew.timer
}

acme_external_http_preflight() {
  local host="$1" token marker url rc
  token="rhmcp-$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"
  marker="RHMCP_ACME_PROBE_$(python3 -c 'import secrets; print(secrets.token_hex(12))')"
  printf '%s\n' "$marker" > "$ACME_WEBROOT/.well-known/acme-challenge/$token"
  url="http://${host}/.well-known/acme-challenge/${token}"
  set +e; external_http_probe "$url" 200; rc=$?; set -e
  rm -f "$ACME_WEBROOT/.well-known/acme-challenge/$token"
  case "$rc" in
    0) ok "External HTTP-01 reachability PASS: $host"; return 0 ;;
    42) fail "External HTTP probe is blocked/filtered (${EXTERNAL_PROBE_BLOCK_HINT:-provider policy}). HTTP-01 will not be attempted."; return 42 ;;
    1) fail 'External nodes reached the target but did not get HTTP 200. HTTP-01 will not be attempted.'; return 1 ;;
    *) fail 'External reachability could not be proven. HTTP-01 will not be attempted.'; return 2 ;;
  esac
}

issue_domain_dns01() {
  local host="$1" email="$2"
  prepare_certbot_hooks cloudflare
  [[ -r "$(cloudflare_dns_token_file)" ]] || die 'Cloudflare DNS token is not configured for DNS-01.'
  CERT_NAME="remote-host-mcp-${host//./-}"
  CERT_RENEWAL_MODE='dns-01-cloudflare'
  certbot_run certonly --non-interactive --agree-tos --email "$email" \
    --cert-name "$CERT_NAME" --manual --preferred-challenges dns \
    --manual-auth-hook "$CERTBOT_AUTH_HOOK" --manual-cleanup-hook "$CERTBOT_CLEANUP_HOOK" \
    -d "$host"
}

issue_domain_http01() {
  local host="$1" email="$2"
  CERT_NAME="remote-host-mcp-${host//./-}"
  CERT_RENEWAL_MODE='http-01-webroot'
  certbot_run certonly --non-interactive --agree-tos --email "$email" \
    --cert-name "$CERT_NAME" --webroot --webroot-path "$ACME_WEBROOT" -d "$host"
}

issue_ip_http01() {
  local ip="$1" email="$2"
  CERT_NAME="remote-host-mcp-ip-${ip//./-}"
  CERT_RENEWAL_MODE='http-01-webroot-shortlived-ip'
  certbot_run certonly --non-interactive --agree-tos --email "$email" \
    --cert-name "$CERT_NAME" --preferred-profile shortlived \
    --webroot --webroot-path "$ACME_WEBROOT" --ip-address "$ip"
}

set_certificate_paths() {
  CERT_FULLCHAIN="$ACME_CONFIG_DIR/live/$CERT_NAME/fullchain.pem"
  CERT_PRIVKEY="$ACME_CONFIG_DIR/live/$CERT_NAME/privkey.pem"
  [[ -r "$CERT_FULLCHAIN" && -r "$CERT_PRIVKEY" ]] || die 'Certificate files were not published by Certbot.'
  record_resource certificate "$CERT_NAME" created
  upsert_env_value "$CONFIG_DIR/rhmcp.env" RHMCP_CERT_NAME "$CERT_NAME"
  upsert_env_value "$CONFIG_DIR/rhmcp.env" RHMCP_CERT_FULLCHAIN "$CERT_FULLCHAIN"
  upsert_env_value "$CONFIG_DIR/rhmcp.env" RHMCP_CERT_RENEWAL_MODE "$CERT_RENEWAL_MODE"
  export CERT_NAME CERT_FULLCHAIN CERT_PRIVKEY CERT_RENEWAL_MODE
}

renewal_dry_run_gate() {
  prepare_certbot_hooks "${RHMCP_DNS_PROVIDER:-cloudflare}"
  certbot_run renew --dry-run --cert-name "$CERT_NAME" --deploy-hook "$CERTBOT_RELOAD_HOOK"
  ok 'certbot renew --dry-run PASS'
}

verify_external_https() {
  local host="$1" port="${2:-443}" url rc
  if [[ "$port" == 443 ]]; then url="https://${host}/health"; else url="https://${host}:${port}/health"; fi
  set +e; external_http_probe "$url" 200; rc=$?; set -e
  [[ $rc -eq 0 ]] || { fail "External HTTPS validation failed for $url"; return 1; }
  ok "External HTTPS PASS: $url"
}

_cloudflare_dns01_available_or_prompt() {
  local mode="$1" token_file
  token_file="$(cloudflare_dns_token_file 2>/dev/null || true)"
  [[ -r "$token_file" ]] && return 0
  [[ "$mode" == cloudflare-dns-only || "$mode" == cloudflare-proxied ]] || return 1
  configure_cloudflare_dns_secret
  [[ -r "$token_file" ]]
}

configure_domain_https() {
  local host="$1" email="$2" dns_mode="${3:-unknown}" port="${4:-443}" probe_rc=0
  ensure_nginx_for_direct
  ensure_product_certbot
  write_managed_nginx_http "$host" "$LOCAL_PORT" "$ACME_WEBROOT"

  if [[ "$dns_mode" == cloudflare-proxied ]]; then
    _cloudflare_dns01_available_or_prompt "$dns_mode" || die 'Cloudflare Proxied + Full(strict) requires DNS-01 automation.'
    issue_domain_dns01 "$host" "$email"
  else
    set +e; acme_external_http_preflight "$host"; probe_rc=$?; set -e
    if [[ $probe_rc -eq 0 ]]; then
      if ! issue_domain_http01 "$host" "$email"; then
        if _cloudflare_dns01_available_or_prompt "$dns_mode"; then
          warn 'HTTP-01 issuance failed; falling back to Cloudflare DNS-01.'
          issue_domain_dns01 "$host" "$email"
        else
          fail 'HTTP-01 issuance failed and no supported DNS-01 provider is configured.'
          return 1
        fi
      fi
    elif _cloudflare_dns01_available_or_prompt "$dns_mode"; then
      warn 'HTTP-01 is unavailable; using DNS-01 fallback. This is certificate validation fallback, not a compliance bypass.'
      issue_domain_dns01 "$host" "$email"
    else
      fail 'HTTP-01 is unavailable and no supported DNS-01 provider credential is configured.'
      return 1
    fi
  fi

  set_certificate_paths
  write_managed_nginx_https "$host" "$LOCAL_PORT" "$ACME_WEBROOT" "$CERT_FULLCHAIN" "$CERT_PRIVKEY" "$port"
  renewal_dry_run_gate
  setup_renewal_timer
  verify_external_https "$host" "$port"
}

configure_public_ip_https() {
  local ip="$1" email="$2" port="${3:-443}"
  valid_ipv4 "$ip" || die 'Public IP HTTPS currently requires an IPv4 address.'
  ensure_nginx_for_direct
  ensure_product_certbot
  write_managed_nginx_http "$ip" "$LOCAL_PORT" "$ACME_WEBROOT"
  acme_external_http_preflight "$ip" || {
    fail 'IP certificates cannot use DNS-01. Fix public HTTP reachability or choose Domain HTTPS / Cloudflare Tunnel / Private.'
    return 1
  }
  issue_ip_http01 "$ip" "$email"
  set_certificate_paths
  write_managed_nginx_https "$ip" "$LOCAL_PORT" "$ACME_WEBROOT" "$CERT_FULLCHAIN" "$CERT_PRIVKEY" "$port"
  renewal_dry_run_gate
  setup_renewal_timer
  verify_external_https "$ip" "$port"
}
