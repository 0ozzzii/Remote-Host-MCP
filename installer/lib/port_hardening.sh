#!/usr/bin/env bash
set -euo pipefail

# Overrides selected lifecycle/TLS functions after the base libraries are sourced.
# Purpose: keep public/client HTTPS port separate from the local Nginx listener,
# allow Domain DNS-01 installs to avoid any public HTTP-01 requirement, and
# provide a bounded portable certificate-renewal scheduler when systemd is absent.

ensure_nginx_for_direct() {
  local require_http80="${1:-true}" https_listen_port="${2:-${HTTPS_LISTEN_PORT:-${PUBLIC_HTTPS_PORT:-443}}}"
  local proxy answer installed=false
  [[ $EUID -eq 0 ]] || die 'Managed direct HTTPS requires root/sudo.'
  proxy="$(detect_proxy)"
  case "$proxy" in
    nginx) record_resource host_nginx "$(command -v nginx)" shared; return 0 ;;
    caddy|apache)
      die "Active ${proxy} detected. Automatic direct-HTTPS takeover is refused; use Cloudflare Tunnel/private mode or integrate manually."
      ;;
  esac

  if [[ "$require_http80" == true ]]; then
    bind_port_free_any 80 || die 'TCP port 80 is already occupied by a non-Nginx listener; refusing managed HTTP-01 takeover.'
  fi
  if [[ "$https_listen_port" != 80 ]]; then
    bind_port_free_any "$https_listen_port" || die "TCP port ${https_listen_port} is already occupied by a non-Nginx listener; refusing managed HTTPS takeover."
  elif [[ "$require_http80" == true ]]; then
    die 'HTTPS listen port 80 conflicts with the HTTP-01 listener.'
  fi

  if ! command -v nginx >/dev/null 2>&1; then
    if [[ "${RHMCP_AUTO_INSTALL_DEPS:-0}" == 1 ]]; then
      install_nginx_dependency
    else
      read -r -p 'Nginx is required for managed HTTPS. Install it now? [y/N]: ' answer || true
      [[ "$answer" =~ ^[Yy]$ ]] || die 'Nginx is required for managed direct HTTPS.'
      install_nginx_dependency
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
    "$venv/bin/python" -m pip install -q --no-cache-dir --upgrade pip
    "$venv/bin/python" -m pip install -q --no-cache-dir 'certbot>=5.4'
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

ensure_managed_nginx_config_marker() {
  local host="$1" config conflict link=''
  config="$(nginx_product_config_path)" || return 1
  conflict="$(nginx_host_conflict "$host" "$config" 2>/dev/null || true)"
  if [[ -n "$conflict" ]]; then
    record_resource nginx_site "$conflict" shared
    fail "Existing Nginx vhost already owns ${host}: ${conflict}. Refusing automatic takeover."
    return 2
  fi
  if [[ -e "$config" ]] && ! managed_file_has_marker "$config"; then
    fail "Foreign Nginx config occupies product path: $config"
    return 2
  fi
  if [[ ! -e "$config" ]]; then
    install -d -m 755 "$(dirname "$config")"
    printf '# Managed-By: remote-host-mcp\n' > "$config"
    chmod 644 "$config"
    record_resource nginx_site "$config" created
  fi
  if [[ "$config" == /etc/nginx/sites-available/* ]]; then
    link="/etc/nginx/sites-enabled/$(basename "$config")"
    if [[ -e "$link" && ! -L "$link" ]]; then fail "Foreign Nginx enabled path exists: $link"; return 2; fi
    ln -sfn "$config" "$link"
    record_resource nginx_site_link "$link" created
  fi
  printf '%s\n' "$config"
}

_portable_proc_start_ticks() {
  local pid="$1" stat rest
  [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/stat" ]] || return 1
  stat="$(cat "/proc/$pid/stat" 2>/dev/null)" || return 1
  rest="${stat##*) }"
  local -a fields=()
  read -r -a fields <<< "$rest"
  [[ "${#fields[@]}" -ge 20 ]] || return 1
  printf '%s\n' "${fields[19]}"
}

portable_renewal_alive() {
  local pidfile="${1:-$LOG_DIR/cert-renew.pid}" loop="${2:-$CERTBOT_DIR/renew-loop.sh}" pid expected current cmdline
  [[ -f "$pidfile" ]] || return 1
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  current="$(_portable_proc_start_ticks "$pid" 2>/dev/null || true)"
  [[ "$current" =~ ^[0-9]+$ ]] || return 1
  expected="$(cat "${pidfile}.start_ticks" 2>/dev/null || true)"
  [[ "$expected" =~ ^[0-9]+$ && "$expected" == "$current" ]] || return 1
  cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  [[ -n "$cmdline" && "$cmdline" == *"$loop"* ]]
}

stop_portable_renewal() {
  local pidfile="${1:-$LOG_DIR/cert-renew.pid}" loop="${2:-$CERTBOT_DIR/renew-loop.sh}" pid
  [[ -f "$pidfile" ]] || { rm -f "${pidfile}.start_ticks"; return 0; }
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    portable_renewal_alive "$pidfile" "$loop" || {
      fail "Refusing to stop portable certificate renewal: PID $pid identity mismatch."
      return 1
    }
    kill "$pid" 2>/dev/null || true
    for _ in {1..30}; do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
    if kill -0 "$pid" 2>/dev/null; then
      portable_renewal_alive "$pidfile" "$loop" || return 1
      kill -9 "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$pidfile" "${pidfile}.start_ticks"
}

setup_portable_renewal() {
  local loop="$CERTBOT_DIR/renew-loop.sh" pidfile="$LOG_DIR/cert-renew.pid" logfile="$LOG_DIR/cert-renew.log"
  local interval="${RHMCP_CERT_RENEW_INTERVAL_S:-43200}" pid ticks
  [[ "$interval" =~ ^[0-9]+$ && "$interval" -ge 60 ]] || die 'Portable certificate renewal interval must be at least 60 seconds.'
  [[ -x "${CERTBOT_RENEW_HOOK:-}" ]] || die 'Certificate renewal hook is unavailable.'
  install -d -m 700 "$CERTBOT_DIR"
  install -d -m 755 "$LOG_DIR"
  cat > "$loop" <<EOF2
#!/usr/bin/env bash
set -u
interval=$(printf '%q' "$interval")
renew_hook=$(printf '%q' "$CERTBOT_RENEW_HOOK")
log_file=$(printf '%q' "$logfile")
child=''
cleanup() {
  if [[ -n "\${child:-}" ]] && kill -0 "\$child" 2>/dev/null; then
    kill "\$child" 2>/dev/null || true
    wait "\$child" 2>/dev/null || true
  fi
  child=''
}
trap 'cleanup; exit 0' TERM INT HUP
while :; do
  sleep "\$interval" &
  child=\$!
  if ! wait "\$child"; then
    child=''
    continue
  fi
  child=''
  /bin/bash "\$renew_hook" >>"\$log_file" 2>&1 || true
done
EOF2
  chmod 700 "$loop"
  record_resource cert_renew_loop "$loop" created
  if portable_renewal_alive "$pidfile" "$loop"; then
    upsert_env_value "$CONFIG_DIR/rhmcp.env" RHMCP_CERT_RENEW_BACKEND portable
    ok 'Portable certificate renewal scheduler already running.'
    return 0
  fi
  stop_portable_renewal "$pidfile" "$loop" || die 'Could not safely clear stale portable certificate-renewal state.'
  nohup /bin/bash "$loop" >/dev/null 2>&1 &
  pid=$!
  ticks="$(_portable_proc_start_ticks "$pid" 2>/dev/null || true)"
  if [[ ! "$ticks" =~ ^[0-9]+$ ]]; then
    kill "$pid" 2>/dev/null || true
    die 'Could not record portable certificate-renewal process identity.'
  fi
  printf '%s\n' "$pid" > "$pidfile"
  printf '%s\n' "$ticks" > "${pidfile}.start_ticks"
  chmod 600 "$pidfile" "${pidfile}.start_ticks"
  sleep 0.1
  portable_renewal_alive "$pidfile" "$loop" || {
    kill "$pid" 2>/dev/null || true
    rm -f "$pidfile" "${pidfile}.start_ticks"
    die 'Portable certificate-renewal scheduler failed to stay running.'
  }
  record_resource portable_cert_renew_pid "$pidfile" created
  upsert_env_value "$CONFIG_DIR/rhmcp.env" RHMCP_CERT_RENEW_BACKEND portable
  ok 'Portable certificate renewal scheduler enabled (12h default interval).'
}

setup_renewal_timer() {
  local service='/etc/systemd/system/remote-host-mcp-cert-renew.service' timer='/etc/systemd/system/remote-host-mcp-cert-renew.timer' tmp
  if ! systemd_operational; then
    setup_portable_renewal
    return
  fi
  [[ $EUID -eq 0 ]] || die 'Managed systemd certificate renewal requires root.'
  stop_portable_renewal "$LOG_DIR/cert-renew.pid" "$CERTBOT_DIR/renew-loop.sh" || die 'Could not safely stop portable certificate renewal before enabling systemd timer.'
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
  upsert_env_value "$CONFIG_DIR/rhmcp.env" RHMCP_CERT_RENEW_BACKEND systemd
}

configure_domain_https() {
  local host="$1" email="$2" dns_mode="${3:-unknown}"
  local listen_port="${4:-${HTTPS_LISTEN_PORT:-443}}" public_port="${5:-${PUBLIC_HTTPS_PORT:-$listen_port}}"
  local challenge_mode="${6:-${DOMAIN_CHALLENGE_MODE:-auto}}" probe_rc=0 http_listener=true dns_only=false

  if [[ "$dns_mode" == cloudflare-proxied || "$challenge_mode" == dns-01 ]]; then
    dns_only=true
    http_listener=false
  fi

  ensure_nginx_for_direct "$([[ "$dns_only" == true ]] && printf false || printf true)" "$listen_port"
  ensure_product_certbot

  if [[ "$dns_only" == true ]]; then
    ensure_managed_nginx_config_marker "$host" >/dev/null
    _cloudflare_dns01_available_or_prompt "$dns_mode" || die 'DNS-01-only mode requires a supported DNS provider credential (Cloudflare currently supported).'
    issue_domain_dns01 "$host" "$email"
  else
    write_managed_nginx_http "$host" "$LOCAL_PORT" "$ACME_WEBROOT"
    set +e; acme_external_http_preflight "$host"; probe_rc=$?; set -e
    if [[ $probe_rc -eq 0 ]]; then
      if ! issue_domain_http01 "$host" "$email"; then
        if _cloudflare_dns01_available_or_prompt "$dns_mode"; then
          warn 'HTTP-01 issuance failed; falling back to Cloudflare DNS-01.'
          issue_domain_dns01 "$host" "$email"
          http_listener=false
        else
          fail 'HTTP-01 issuance failed and no supported DNS-01 provider is configured.'
          return 1
        fi
      fi
    elif _cloudflare_dns01_available_or_prompt "$dns_mode"; then
      warn 'HTTP-01 is unavailable; using DNS-01 fallback. This is certificate validation fallback, not a compliance bypass.'
      issue_domain_dns01 "$host" "$email"
      http_listener=false
    else
      fail 'HTTP-01 is unavailable and no supported DNS-01 provider credential is configured.'
      return 1
    fi
  fi

  set_certificate_paths
  [[ "$CERT_RENEWAL_MODE" == http-01-* ]] || http_listener=false
  write_managed_nginx_https "$host" "$LOCAL_PORT" "$ACME_WEBROOT" "$CERT_FULLCHAIN" "$CERT_PRIVKEY" "$listen_port" "$public_port" "$http_listener"
  renewal_dry_run_gate
  setup_renewal_timer
  verify_external_https "$host" "$public_port"
}

configure_public_ip_https() {
  local ip="$1" email="$2" listen_port="${3:-${HTTPS_LISTEN_PORT:-443}}" public_port="${4:-${PUBLIC_HTTPS_PORT:-$listen_port}}"
  valid_ipv4 "$ip" || die 'Public IP HTTPS currently requires an IPv4 address.'
  ensure_nginx_for_direct true "$listen_port"
  ensure_product_certbot
  write_managed_nginx_http "$ip" "$LOCAL_PORT" "$ACME_WEBROOT"
  acme_external_http_preflight "$ip" || {
    fail 'IP certificates cannot use DNS-01. Fix public HTTP reachability or choose Domain HTTPS / Cloudflare Tunnel / Private.'
    return 1
  }
  issue_ip_http01 "$ip" "$email"
  set_certificate_paths
  write_managed_nginx_https "$ip" "$LOCAL_PORT" "$ACME_WEBROOT" "$CERT_FULLCHAIN" "$CERT_PRIVKEY" "$listen_port" "$public_port" true
  renewal_dry_run_gate
  setup_renewal_timer
  verify_external_https "$ip" "$public_port"
}

load_repair_runtime_context() {
  local env_file="$CONFIG_DIR/rhmcp.env"
  [[ -f "$env_file" ]] || die 'Runtime environment is missing; automatic secret reconstruction is intentionally refused.'
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
  AUTH_MODE="${RHMCP_AUTH_MODE:-${AUTH_MODE:-capability}}"
  PATH_KEY="${RHMCP_PATH_KEY:-${PATH_KEY:-}}"
  PUBLIC_HOST="${RHMCP_PUBLIC_HOST:-${PUBLIC_HOST:-mcp.invalid}}"
  LOCAL_PORT="${RHMCP_PORT:-${LOCAL_PORT:-8765}}"
  PUBLIC_HTTPS_PORT="${RHMCP_PUBLIC_HTTPS_PORT:-${PUBLIC_HTTPS_PORT:-443}}"
  HTTPS_LISTEN_PORT="${RHMCP_HTTPS_LISTEN_PORT:-${HTTPS_LISTEN_PORT:-$PUBLIC_HTTPS_PORT}}"
  DOMAIN_DNS_MODE="${RHMCP_DOMAIN_DNS_MODE:-${DOMAIN_DNS_MODE:-unknown}}"
  DOMAIN_CHALLENGE_MODE="${RHMCP_DOMAIN_CHALLENGE_MODE:-${DOMAIN_CHALLENGE_MODE:-auto}}"
  ACME_EMAIL="${RHMCP_ACME_EMAIL:-${ACME_EMAIL:-}}"
  export AUTH_MODE PATH_KEY PUBLIC_HOST LOCAL_PORT PUBLIC_HTTPS_PORT HTTPS_LISTEN_PORT DOMAIN_DNS_MODE DOMAIN_CHALLENGE_MODE ACME_EMAIL
}

repair_ingress_lifecycle() {
  local token_file rc cert_name cert_fullchain cert_privkey cert_mode config http_listener=true
  case "${INGRESS:-private}" in
    private)
      info 'Private/local profile: no public ingress repair required.'
      ;;
    cloudflare-tunnel)
      token_file="$SECRET_DIR/cloudflared.token"
      [[ -r "$token_file" ]] || die 'Cloudflare Tunnel credential is missing; repair will not reconstruct secrets. Restore the credential or reconfigure the tunnel.'
      install_managed_tunnel_service "$token_file"
      set +e; external_http_probe "https://${PUBLIC_HOST}/health" 200; rc=$?; set -e
      [[ $rc -eq 0 ]] || { fail 'Cloudflare Tunnel repair did not pass external HTTPS validation.'; return 1; }
      ok 'Cloudflare Tunnel repair + external HTTPS PASS'
      ;;
    domain|public-ip)
      [[ $EUID -eq 0 ]] || die 'Managed direct HTTPS repair requires root/sudo.'
      [[ -n "$ACME_EMAIL" && "$ACME_EMAIL" == *@*.* ]] || die 'Stored ACME contact email is missing or invalid; refusing certificate repair.'
      ensure_product_certbot
      cert_name="${RHMCP_CERT_NAME:-}"
      cert_fullchain="${RHMCP_CERT_FULLCHAIN:-}"
      cert_mode="${RHMCP_CERT_RENEWAL_MODE:-}"
      cert_privkey=''
      [[ -n "$cert_name" ]] && cert_privkey="$ACME_CONFIG_DIR/live/$cert_name/privkey.pem"
      config="$(nginx_product_config_path 2>/dev/null || true)"
      if [[ "${INGRESS:-}" == public-ip || "$cert_mode" == http-01-* ]]; then http_listener=true; else http_listener=false; fi
      ensure_nginx_for_direct "$http_listener" "$HTTPS_LISTEN_PORT"
      if [[ -n "$cert_name" && -r "$cert_fullchain" && -r "$cert_privkey" ]]; then
        CERT_NAME="$cert_name"
        CERT_FULLCHAIN="$cert_fullchain"
        CERT_PRIVKEY="$cert_privkey"
        CERT_RENEWAL_MODE="$cert_mode"
        export CERT_NAME CERT_FULLCHAIN CERT_PRIVKEY CERT_RENEWAL_MODE
        [[ -n "$config" ]] || die 'No supported Nginx product config path is available for repair.'
        if [[ -e "$config" ]] && ! managed_file_has_marker "$config"; then die "Foreign Nginx config occupies product path: $config"; fi
        if [[ ! -e "$config" && "$http_listener" == true ]]; then write_managed_nginx_http "$PUBLIC_HOST" "$LOCAL_PORT" "$ACME_WEBROOT"; fi
        if [[ ! -e "$config" && "$http_listener" == false ]]; then ensure_managed_nginx_config_marker "$PUBLIC_HOST" >/dev/null; fi
        write_managed_nginx_https "$PUBLIC_HOST" "$LOCAL_PORT" "$ACME_WEBROOT" "$CERT_FULLCHAIN" "$CERT_PRIVKEY" "$HTTPS_LISTEN_PORT" "$PUBLIC_HTTPS_PORT" "$http_listener"
        renewal_dry_run_gate
        setup_renewal_timer
        verify_external_https "$PUBLIC_HOST" "$PUBLIC_HTTPS_PORT"
      elif [[ "$INGRESS" == domain ]]; then
        warn 'Stored certificate material is incomplete; re-running managed Domain HTTPS issuance using persisted non-secret settings and existing provider credential if required.'
        configure_domain_https "$PUBLIC_HOST" "$ACME_EMAIL" "$DOMAIN_DNS_MODE" "$HTTPS_LISTEN_PORT" "$PUBLIC_HTTPS_PORT" "$DOMAIN_CHALLENGE_MODE"
      else
        warn 'Stored IP certificate material is incomplete; re-running short-lived Public IP HTTPS issuance.'
        configure_public_ip_https "$PUBLIC_HOST" "$ACME_EMAIL" "$HTTPS_LISTEN_PORT" "$PUBLIC_HTTPS_PORT"
      fi
      ok 'Managed direct HTTPS repair PASS'
      ;;
    *) die "Unknown ingress profile in install state: ${INGRESS:-unset}" ;;
  esac
}
