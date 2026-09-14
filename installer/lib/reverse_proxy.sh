#!/usr/bin/env bash
set -euo pipefail

detect_proxy() {
  if command -v nginx >/dev/null 2>&1 && (systemctl is-active nginx >/dev/null 2>&1 || pgrep -x nginx >/dev/null 2>&1); then printf 'nginx\n'; return 0; fi
  if command -v caddy >/dev/null 2>&1 && (systemctl is-active caddy >/dev/null 2>&1 || pgrep -x caddy >/dev/null 2>&1); then printf 'caddy\n'; return 0; fi
  if command -v apache2 >/dev/null 2>&1 && (systemctl is-active apache2 >/dev/null 2>&1 || pgrep -x apache2 >/dev/null 2>&1); then printf 'apache\n'; return 0; fi
  printf 'none\n'
}

generate_nginx_snippet() {
  local host="$1" port="$2" out="$3"
  cat > "$out" <<EOF2
# Remote Host MCP generated reverse-proxy template.
server {
    listen 80;
    server_name ${host};
    location / { proxy_pass http://127.0.0.1:${port}; }
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

nginx_product_config_path() {
  if [[ -n "${RHMCP_NGINX_CONFIG_PATH:-}" ]]; then
    [[ "$RHMCP_NGINX_CONFIG_PATH" = /* ]] || return 1
    printf '%s\n' "$RHMCP_NGINX_CONFIG_PATH"
  elif [[ -d /etc/nginx/conf.d ]]; then
    printf '/etc/nginx/conf.d/remote-host-mcp.conf\n'
  elif [[ -d /etc/nginx/sites-available ]]; then
    printf '/etc/nginx/sites-available/remote-host-mcp.conf\n'
  else
    return 1
  fi
}

nginx_file_owns_host() {
  local file="$1" host="$2"
  awk -v host="$host" '
    /^[[:space:]]*server_name[[:space:]]/ {
      for (i=2; i<=NF; i++) {
        token=$i; gsub(/;/, "", token)
        if (token == host) found=1
      }
    }
    END { exit(found ? 0 : 1) }
  ' "$file" 2>/dev/null
}

nginx_host_conflict() {
  local host="$1" ours="$2" file
  for file in /etc/nginx/conf.d/*.conf /etc/nginx/sites-enabled/* /etc/nginx/sites-available/*; do
    [[ -f "$file" && "$file" != "$ours" ]] || continue
    if nginx_file_owns_host "$file" "$host"; then printf '%s\n' "$file"; return 0; fi
  done
  return 1
}

reload_nginx_safely() {
  nginx -t
  if systemd_operational && systemctl is-active nginx >/dev/null 2>&1; then
    systemctl reload nginx
  else
    nginx -s reload 2>/dev/null || nginx
  fi
}

write_managed_nginx_http() {
  local host="$1" port="$2" webroot="$3" config tmp conflict link=''
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
  install -d -m 755 "$webroot/.well-known/acme-challenge"
  install -d -m 755 "$(dirname "$config")"
  tmp="${config}.tmp.$$"
  cat > "$tmp" <<EOF2
# Managed-By: remote-host-mcp
server {
    listen 80;
    server_name ${host};

    location ^~ /.well-known/acme-challenge/ {
        root ${webroot};
        default_type text/plain;
    }

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
  chmod 644 "$tmp"; mv -f "$tmp" "$config"
  record_resource nginx_site "$config" created
  if [[ "$config" == /etc/nginx/sites-available/* ]]; then
    link="/etc/nginx/sites-enabled/$(basename "$config")"
    if [[ -e "$link" && ! -L "$link" ]]; then fail "Foreign Nginx enabled path exists: $link"; return 2; fi
    ln -sfn "$config" "$link"
    record_resource nginx_site_link "$link" created
  fi
  reload_nginx_safely
}

write_managed_nginx_https() {
  local host="$1" port="$2" webroot="$3" cert="$4" key="$5"
  local https_listen_port="${6:-443}" public_https_port="${7:-${6:-443}}" http_listener="${8:-true}"
  local config tmp redirect_port=''
  config="$(nginx_product_config_path)" || return 1
  managed_file_has_marker "$config" || return 2
  [[ -r "$cert" && -r "$key" ]] || return 1
  [[ "$https_listen_port" =~ ^[0-9]+$ && "$https_listen_port" -ge 1 && "$https_listen_port" -le 65535 ]] || return 2
  [[ "$public_https_port" =~ ^[0-9]+$ && "$public_https_port" -ge 1 && "$public_https_port" -le 65535 ]] || return 2
  [[ "$http_listener" == true || "$http_listener" == false ]] || return 2
  if [[ "$http_listener" == true && "$https_listen_port" == 80 ]]; then
    fail 'HTTPS listen port 80 conflicts with the required HTTP-01 listener.'
    return 2
  fi
  [[ "$public_https_port" == 443 ]] || redirect_port=":${public_https_port}"
  tmp="${config}.tmp.$$"
  : > "$tmp"
  printf '# Managed-By: remote-host-mcp\n' >> "$tmp"
  if [[ "$http_listener" == true ]]; then
    cat >> "$tmp" <<EOF2
server {
    listen 80;
    server_name ${host};

    location ^~ /.well-known/acme-challenge/ {
        root ${webroot};
        default_type text/plain;
    }
    location / { return 308 https://\$host${redirect_port}\$request_uri; }
}

EOF2
  fi
  cat >> "$tmp" <<EOF2
server {
    listen ${https_listen_port} ssl;
    server_name ${host};
    ssl_certificate ${cert};
    ssl_certificate_key ${key};
    ssl_protocols TLSv1.2 TLSv1.3;

    location / {
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_buffering off;
        proxy_cache off;
        proxy_pass http://127.0.0.1:${port};
    }
}
EOF2
  chmod 644 "$tmp"; mv -f "$tmp" "$config"
  reload_nginx_safely
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
      DIRECT_READY=false
      ;;
    caddy)
      generated="$CONFIG_DIR/generated/Caddyfile.remote-host-mcp"
      generate_caddy_block "$host" "$port" "$generated"
      info "Existing Caddy detected. Managed block generated: $generated"
      DIRECT_READY=false
      ;;
    apache)
      warn 'Existing Apache detected. Automatic modification is not enabled; no existing virtual host was changed.'
      DIRECT_READY=false
      ;;
    none)
      generated="$CONFIG_DIR/generated/Caddyfile.remote-host-mcp"
      generate_caddy_block "$host" "$port" "$generated"
      warn "No reverse proxy detected. Caddy configuration generated: $generated"
      DIRECT_READY=false
      ;;
  esac
  REVERSE_PROXY="$proxy"; export DIRECT_READY REVERSE_PROXY
}
