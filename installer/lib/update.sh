#!/usr/bin/env bash
set -euo pipefail

RHMCP_REPO_RAW_BASE="${RHMCP_REPO_RAW_BASE:-https://raw.githubusercontent.com/0ozzzii/Remote-Host-MCP/main}"

latest_version() {
  curl -fsSL --connect-timeout 8 --max-time 15 "$RHMCP_REPO_RAW_BASE/VERSION" 2>/dev/null | tr -d '\r\n'
}

version_is_newer() {
  local current="$1" remote="$2"
  python3 - "$current" "$remote" <<'PY'
import re, sys

def key(v):
    m=re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?', v)
    if not m: return None
    major,minor,patch=map(int,m.group(1,2,3))
    stage={'alpha':0,'beta':1,'rc':2,None:3}[m.group(4)]
    n=int(m.group(5) or 0)
    return major,minor,patch,stage,n
ka,kb=key(sys.argv[1]),key(sys.argv[2])
if ka is None or kb is None: raise SystemExit(2)
raise SystemExit(0 if kb > ka else 1)
PY
}
