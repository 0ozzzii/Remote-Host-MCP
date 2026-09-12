#!/usr/bin/env bash
set -euo pipefail

detect_proxy() {
  if command -v nginx >/dev/null 2>&1 && (systemctl is-active nginx >/dev/null 2>&1 || pgrep -x nginx >/dev/null 2>&1); then
    printf 'nginx\n'; return 0
  fi
  if command -v caddy >/dev/null 2>&1 && (systemctl is-active caddy >/dev/null 2>&1 || pgrep -x caddy >/dev/null 2>&1); then
    printf 'caddy\n'; return 0
  fi
  if command -v apache2 >/dev/null 2>&1 && (systemctl is-active apache2 >/dev/null 2>&1 || pgrep -x apache2 >/dev/null 2>&1); then
    printf 'apache\n'; return 0
  fi
  printf 'none\n'
}

generate_nginx_snippet() {
  local host="$1" port="$2" out="$3"
  cat > "$out" <<EOF2
# Remote Host MCP generated reverse-proxy template.
# Add TLS directives using your existing certificate manager before enabling publicly.
server {
    listen 80;
    server_name ${host};

    location / {
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 3600s;
        proxy_buffering off;
        proxy_pass http://127.0.0.1:${port};
    }
}
EOF2
}

generate_caddy_block() {
  local host="$1" port="$2" out="$3"
  cat > "$out" <<EOF2
${host} {
    reverse_proxy 127.0.0.1:${port}
}
EOF2
}

prepare_direct_ingress() {
  local host="$1" port="$2" proxy generated
  proxy="$(detect_proxy)"
  install -d "$CONFIG_DIR/generated"
  case "$proxy" in
    nginx)
      generated="$CONFIG_DIR/generated/nginx-remote-host-mcp.conf"
      generate_nginx_snippet "$host" "$port" "$generated"
      warn "Existing Nginx detected. Safe template generated: $generated"
      warn 'Automatic TLS takeover is intentionally not forced; reuse the existing certificate/ACME system, then proxy this hostname to localhost.'
      DIRECT_READY=false
      ;;
    caddy)
      generated="$CONFIG_DIR/generated/Caddyfile.remote-host-mcp"
      generate_caddy_block "$host" "$port" "$generated"
      info "Existing Caddy detected. Managed block generated: $generated"
      DIRECT_READY=false
      ;;
    apache)
      warn 'Existing Apache detected. Automatic modification is not enabled in this alpha; no existing virtual host was changed.'
      DIRECT_READY=false
      ;;
    none)
      generated="$CONFIG_DIR/generated/Caddyfile.remote-host-mcp"
      generate_caddy_block "$host" "$port" "$generated"
      warn "No reverse proxy detected. Caddy configuration generated: $generated"
      warn 'This alpha does not silently install a new web server. Review/install Caddy, then apply the generated block.'
      DIRECT_READY=false
      ;;
  esac
  REVERSE_PROXY="$proxy"
  export DIRECT_READY REVERSE_PROXY
}
