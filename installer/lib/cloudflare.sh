#!/usr/bin/env bash
set -euo pipefail

CLOUDFLARED_PINNED_VERSION='2026.9.1'
CLOUDFLARED_LINUX_AMD64_SHA256='03f1f25d1cc93b9ad6c60569d44060bc4f17ed97075760ed8cfca4b12dcd68cc'
CLOUDFLARED_LINUX_ARM64_SHA256='3d97437c71848bd8df68041e12436b484a661d95073ea1937f01a845ce88faa3'

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

cloudflared_asset_for_arch() {
  local arch="$1"
  case "$arch" in
    x86_64|amd64)
      printf 'cloudflared-linux-amd64|%s\n' "$CLOUDFLARED_LINUX_AMD64_SHA256"
      ;;
    aarch64|arm64)
      printf 'cloudflared-linux-arm64|%s\n' "$CLOUDFLARED_LINUX_ARM64_SHA256"
      ;;
    *) return 1 ;;
  esac
}

install_pinned_cloudflared() {
  local runtime_dir="$1" pair asset expected bin tmp url actual
  command -v curl >/dev/null 2>&1 || return 1
  command -v sha256sum >/dev/null 2>&1 || return 1
  pair="$(cloudflared_asset_for_arch "$(uname -m)")" || return 1
  asset="${pair%%|*}"
  expected="${pair#*|}"
  bin="$runtime_dir/bin/cloudflared"
  mkdir -p "$runtime_dir/bin"
  tmp="$(mktemp "$runtime_dir/bin/cloudflared.tmp.XXXXXX")"
  url="https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_PINNED_VERSION}/${asset}"
  if ! curl -fL --retry 2 --connect-timeout 10 -o "$tmp" "$url" >/dev/null 2>&1; then
    rm -f "$tmp"
    return 1
  fi
  actual="$(sha256sum "$tmp" | awk '{print $1}')"
  if [[ "$actual" != "$expected" ]]; then
    rm -f "$tmp"
    printf 'cloudflared checksum mismatch: expected %s, got %s\n' "$expected" "$actual" >&2
    return 1
  fi
  chmod 755 "$tmp"
  if ! "$tmp" --version 2>&1 | grep -Fq "$CLOUDFLARED_PINNED_VERSION"; then
    rm -f "$tmp"
    printf 'cloudflared version verification failed for %s\n' "$CLOUDFLARED_PINNED_VERSION" >&2
    return 1
  fi
  mv "$tmp" "$bin"
  printf '%s\n' "$bin"
}

ensure_cloudflared_binary() {
  local runtime_dir="$1" bin
  if command -v cloudflared >/dev/null 2>&1; then
    command -v cloudflared
    return 0
  fi
  bin="$runtime_dir/bin/cloudflared"
  if [[ -x "$bin" ]]; then
    printf '%s\n' "$bin"
    return 0
  fi
  install_pinned_cloudflared "$runtime_dir"
}
