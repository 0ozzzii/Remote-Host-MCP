#!/usr/bin/env bash
set -euo pipefail

extract_cloudflare_token() {
  local input="$1" token
  # Accept a raw token, `cloudflared tunnel run --token TOKEN`, or
  # `cloudflared service install TOKEN`. Never execute user input as shell code.
  if [[ "$input" =~ ^[A-Za-z0-9._=-]{40,}$ ]]; then
    token="$input"
  elif [[ "$input" =~ cloudflared[[:space:]]+service[[:space:]]+install[[:space:]]+([^[:space:];|&]+) ]]; then
    token="${BASH_REMATCH[1]}"
  elif [[ "$input" =~ --token[=[:space:]]+([^[:space:];|&]+) ]]; then
    token="${BASH_REMATCH[1]}"
  else
    return 1
  fi
  [[ "$token" =~ ^[A-Za-z0-9._=-]{40,}$ ]] || return 1
  printf '%s\n' "$token"
}

save_cloudflare_token() {
  local token="$1" secret_dir="$2" file
  file="$secret_dir/cloudflared.token"
  install -d -m 700 "$secret_dir"
  umask 077
  printf '%s\n' "$token" > "$file"
  chmod 600 "$file"
  if declare -F record_resource >/dev/null 2>&1; then record_resource cloudflare_tunnel_token "$file" created; fi
  printf '%s\n' "$file"
}

ensure_cloudflared_binary() {
  local runtime_dir="$1" bin arch cf_arch tmp
  if command -v cloudflared >/dev/null 2>&1; then
    if declare -F record_resource >/dev/null 2>&1; then record_resource host_cloudflared "$(command -v cloudflared)" shared; fi
    command -v cloudflared
    return 0
  fi
  bin="$runtime_dir/bin/cloudflared"
  if [[ -x "$bin" ]]; then printf '%s\n' "$bin"; return 0; fi
  mkdir -p "$runtime_dir/bin"
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) cf_arch=amd64 ;;
    aarch64|arm64) cf_arch=arm64 ;;
    *) return 1 ;;
  esac
  tmp="$(mktemp "$runtime_dir/bin/cloudflared.tmp.XXXXXX")"
  if ! curl -fL --retry 2 --connect-timeout 10 \
      -o "$tmp" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${cf_arch}" >/dev/null 2>&1; then
    rm -f "$tmp"
    return 1
  fi
  chmod 755 "$tmp"
  mv "$tmp" "$bin"
  if declare -F record_resource >/dev/null 2>&1; then record_resource cloudflared_binary "$bin" created; fi
  printf '%s\n' "$bin"
}

install_managed_tunnel_service() {
  local token_file="$1" cf unit='/etc/systemd/system/remote-host-mcp-tunnel.service' tmp
  cf="$(ensure_cloudflared_binary "$RUNTIME_DIR")" || die 'Could not provide cloudflared binary.'
  if [[ "$INSTALL_MODE" == system && $EUID -eq 0 ]] && systemd_operational; then
    if [[ -e "$unit" ]] && ! managed_file_has_marker "$unit"; then die "Foreign tunnel service unit exists: $unit"; fi
    tmp="${unit}.tmp.$$"
    cat > "$tmp" <<EOF2
# Managed-By: remote-host-mcp
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
    chmod 644 "$tmp"; mv -f "$tmp" "$unit"
    record_resource tunnel_service_unit "$unit" created
    systemctl daemon-reload
    systemctl enable --now remote-host-mcp-tunnel.service
  else
    (cd "$CURRENT_LINK" && bash scripts/manage.sh tunnel-restart >/dev/null)
    record_resource portable_tunnel_pid "$LOG_DIR/tunnel.pid" created
  fi
}

configure_managed_cloudflare_tunnel() {
  local host="$1" token_input="$2" token token_file rc
  token="$(extract_cloudflare_token "$token_input")" || die 'Could not parse a valid Cloudflare Tunnel token.'
  token_file="$(save_cloudflare_token "$token" "$SECRET_DIR")"; unset token token_input
  install_managed_tunnel_service "$token_file"
  info "Cloudflare Published Application must route ${host} to http://127.0.0.1:${LOCAL_PORT}"
  set +e; external_http_probe "https://${host}/health" 200; rc=$?; set -e
  case "$rc" in
    0) PUBLIC_READY=true; ok 'Cloudflare Tunnel external HTTPS PASS' ;;
    42) fail "Cloudflare public endpoint returned a provider/policy block: ${EXTERNAL_PROBE_BLOCK_HINT:-HTTP 403}"; return 1 ;;
    *) fail 'Cloudflare Tunnel is running but external HTTPS is not ready. Verify Published Application hostname/origin.'; return 1 ;;
  esac
}
