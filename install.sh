#!/usr/bin/env bash
set -euo pipefail

REPO='0ozzzii/Remote-Host-MCP'
REF="${RHMCP_INSTALL_REF:-main}"
EXPECTED_SHA256="${RHMCP_SOURCE_SHA256:-}"

if [[ -f "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/installer/install.sh" ]]; then
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  export RHMCP_SOURCE_ROOT="$ROOT"
  exec bash "$ROOT/installer/install.sh" "$@"
fi

command -v curl >/dev/null 2>&1 || { echo 'curl is required / 需要 curl' >&2; exit 1; }
command -v tar >/dev/null 2>&1 || { echo 'tar is required / 需要 tar' >&2; exit 1; }
command -v sha256sum >/dev/null 2>&1 || { echo 'sha256sum is required / 需要 sha256sum' >&2; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
archive="$tmp/source.tar.gz"
url="https://github.com/${REPO}/archive/${REF}.tar.gz"

curl -fsSL --retry 2 --connect-timeout 10 "$url" -o "$archive"

if [[ -n "$EXPECTED_SHA256" ]]; then
  [[ "$EXPECTED_SHA256" =~ ^[a-fA-F0-9]{64}$ ]] || {
    echo 'RHMCP_SOURCE_SHA256 must be exactly 64 hexadecimal characters' >&2
    exit 1
  }
  actual="$(sha256sum "$archive" | awk '{print $1}')"
  [[ "${actual,,}" == "${EXPECTED_SHA256,,}" ]] || {
    echo "Source archive SHA-256 mismatch for ref: $REF" >&2
    exit 1
  }
fi

if tar -tzf "$archive" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
  echo 'Unsafe path detected in source archive' >&2
  exit 1
fi

tar -xzf "$archive" --no-same-owner --no-same-permissions -C "$tmp"
root="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -1)"
[[ -n "$root" && -f "$root/installer/install.sh" ]] || { echo 'Invalid source archive' >&2; exit 1; }

export RHMCP_SOURCE_ROOT="$root"
exec bash "$root/installer/install.sh" "$@"
