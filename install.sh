#!/usr/bin/env bash
set -euo pipefail

REPO='0ozzzii/Remote-Host-MCP'
REF="${RHMCP_INSTALL_REF:-main}"

if [[ -f "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/installer/install.sh" ]]; then
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  export RHMCP_SOURCE_ROOT="$ROOT"
  if git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    export RHMCP_REQUESTED_REF="${RHMCP_REQUESTED_REF:-$(git -C "$ROOT" symbolic-ref -q --short HEAD 2>/dev/null || git -C "$ROOT" rev-parse HEAD)}"
    export RHMCP_RESOLVED_COMMIT="${RHMCP_RESOLVED_COMMIT:-$(git -C "$ROOT" rev-parse HEAD)}"
  fi
  exec bash "$ROOT/installer/install.sh" "$@"
fi

command -v curl >/dev/null 2>&1 || { echo 'curl is required / 需要 curl' >&2; exit 1; }
command -v tar >/dev/null 2>&1 || { echo 'tar is required / 需要 tar' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo 'python3 is required / 需要 python3' >&2; exit 1; }

encoded_ref="$(python3 - "$REF" <<'PY'
from urllib.parse import quote
import sys
print(quote(sys.argv[1], safe=''))
PY
)"
resolved="$(curl -fsSL --retry 2 --connect-timeout 10 \
  -H 'Accept: application/vnd.github+json' \
  "https://api.github.com/repos/${REPO}/commits/${encoded_ref}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("sha", ""))')"
[[ "$resolved" =~ ^[0-9a-fA-F]{40}$ ]] || { echo 'Could not resolve requested GitHub ref to a full commit SHA. / 无法把安装 ref 固定解析为完整 commit。' >&2; exit 1; }

export RHMCP_REQUESTED_REF="$REF"
export RHMCP_RESOLVED_COMMIT="${resolved,,}"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
archive="$tmp/source.tar.gz"
curl -fsSL --retry 2 --connect-timeout 10 "https://github.com/${REPO}/archive/${RHMCP_RESOLVED_COMMIT}.tar.gz" -o "$archive"
tar -xzf "$archive" -C "$tmp"
root="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -1)"
[[ -n "$root" && -f "$root/installer/install.sh" ]] || { echo 'Invalid source archive' >&2; exit 1; }
export RHMCP_SOURCE_ROOT="$root"
exec bash "$root/installer/install.sh" "$@"
