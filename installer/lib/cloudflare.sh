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
  printf '%s\n' "$file"
}

ensure_cloudflared_binary() {
  local runtime_dir="$1" bin arch cf_arch tmp
  if command -v cloudflared >/dev/null 2>&1; then
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
  printf '%s\n' "$bin"
}
