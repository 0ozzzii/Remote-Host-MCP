#!/usr/bin/env bash
set -euo pipefail

REPO='0ozzzii/Remote-Host-MCP'
REF="${RHMCP_INSTALL_REF:-main}"

if [[ -f "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/installer/install.sh" ]]; then
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  export RHMCP_SOURCE_ROOT="$ROOT"
  exec bash "$ROOT/installer/install.sh" "$@"
fi

command -v curl >/dev/null 2>&1 || { echo 'curl is required / 需要 curl' >&2; exit 1; }
command -v tar >/dev/null 2>&1 || { echo 'tar is required / 需要 tar' >&2; exit 1; }
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
archive="$tmp/source.tar.gz"
curl -fsSL --retry 2 --connect-timeout 10 "https://github.com/${REPO}/archive/${REF}.tar.gz" -o "$archive"
tar -xzf "$archive" -C "$tmp"
root="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -1)"
[[ -n "$root" && -f "$root/installer/install.sh" ]] || { echo 'Invalid source archive' >&2; exit 1; }
export RHMCP_SOURCE_ROOT="$root"
exec bash "$root/installer/install.sh" "$@"
