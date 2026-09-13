#!/usr/bin/env bash
set -euo pipefail

EXTERNAL_PROBE_LAST=''
EXTERNAL_PROBE_BLOCK_HINT=''

_provider_block_fingerprint() {
  local text="$1"
  grep -Eqi 'server[" :]+beaver|non-compliance[[:space:]]+icp[[:space:]]+filing|beian-block|icp[[:space:]_-]*filing' <<<"$text"
}

_checkhost_dispatch() {
  local target="$1" encoded
  encoded="$(python3 - "$target" <<'PY'
from urllib.parse import quote
import sys
print(quote(sys.argv[1], safe=''))
PY
)"
  curl -fsS --max-time 12 -H 'Accept: application/json' \
    "https://check-host.net/check-http?host=${encoded}&max_nodes=3"
}

_checkhost_result() {
  local request_id="$1"
  curl -fsS --max-time 12 -H 'Accept: application/json' \
    "https://check-host.net/check-result/${request_id}"
}

external_http_probe() {
  local target="$1" expected="${2:-200}" dispatch request_id result='' attempt parsed
  EXTERNAL_PROBE_LAST=''; EXTERNAL_PROBE_BLOCK_HINT=''

  if [[ -n "${RHMCP_EXTERNAL_PROBE_URL:-}" ]]; then
    local encoded endpoint
    [[ "$RHMCP_EXTERNAL_PROBE_URL" == https://* ]] || return 2
    encoded="$(python3 - "$target" <<'PY'
from urllib.parse import quote
import sys
print(quote(sys.argv[1], safe=''))
PY
)"
    if [[ "$RHMCP_EXTERNAL_PROBE_URL" == *'{url}'* ]]; then
      endpoint="${RHMCP_EXTERNAL_PROBE_URL//\{url\}/$encoded}"
    else
      endpoint="${RHMCP_EXTERNAL_PROBE_URL}${RHMCP_EXTERNAL_PROBE_URL#*\?}" # replaced below when no query
      if [[ "$RHMCP_EXTERNAL_PROBE_URL" == *\?* ]]; then endpoint="${RHMCP_EXTERNAL_PROBE_URL}&url=${encoded}"; else endpoint="${RHMCP_EXTERNAL_PROBE_URL}?url=${encoded}"; fi
    fi
    result="$(curl -fsS --max-time 15 "$endpoint")" || return 2
    EXTERNAL_PROBE_LAST="$result"
    if _provider_block_fingerprint "$result"; then EXTERNAL_PROBE_BLOCK_HINT='PROVIDER_POLICY_BLOCK_FINGERPRINT'; return 42; fi
    parsed="$(python3 - "$expected" <<'PY' <<<"$result" 2>/dev/null || true
import json,sys
expected=int(sys.argv[1])
try:
    x=json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
status=x.get('status') or x.get('status_code') or x.get('http_status')
reachable=x.get('reachable', True)
print('pass' if reachable and int(status)==expected else ('blocked' if int(status or 0)==403 else 'fail'))
PY
)"
    [[ "$parsed" == pass ]] && return 0
    [[ "$parsed" == blocked ]] && { EXTERNAL_PROBE_BLOCK_HINT='HTTP_403_PROVIDER_OR_POLICY_BLOCK'; return 42; }
    return 1
  fi

  dispatch="$(_checkhost_dispatch "$target")" || return 2
  request_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("request_id", ""))' <<<"$dispatch" 2>/dev/null || true)"
  [[ "$request_id" =~ ^[A-Za-z0-9-]+$ ]] || return 2
  for attempt in 1 2 3 4 5 6; do
    result="$(_checkhost_result "$request_id" 2>/dev/null || true)"
    [[ -n "$result" ]] || { sleep 2; continue; }
    parsed="$(python3 - "$expected" <<'PY' <<<"$result" 2>/dev/null || true
import json,sys
expected=str(int(sys.argv[1]))
data=json.load(sys.stdin)
statuses=[]
for value in data.values():
    if not value:
        continue
    rows=value if isinstance(value, list) else [value]
    for row in rows:
        if isinstance(row, list) and row and isinstance(row[0], list):
            row=row[0]
        if isinstance(row, list) and len(row)>=4 and row[3] is not None:
            statuses.append(str(row[3]))
passed=sum(s==expected for s in statuses)
blocked=sum(s=='403' for s in statuses)
complete=len(statuses)
print(f'{passed}:{blocked}:{complete}')
PY
)"
    EXTERNAL_PROBE_LAST="$result"
    if [[ "$parsed" =~ ^([0-9]+):([0-9]+):([0-9]+)$ ]]; then
      if (( BASH_REMATCH[1] >= 2 )); then return 0; fi
      if (( BASH_REMATCH[2] >= 1 && BASH_REMATCH[3] >= 2 )); then EXTERNAL_PROBE_BLOCK_HINT='HTTP_403_PROVIDER_OR_POLICY_BLOCK'; return 42; fi
      if (( BASH_REMATCH[3] >= 2 )); then return 1; fi
    fi
    sleep 2
  done
  return 2
}

external_tcp_probe() {
  local host="$1" port="$2" encoded dispatch request_id result attempt passed complete
  encoded="$(python3 - "${host}:${port}" <<'PY'
from urllib.parse import quote
import sys
print(quote(sys.argv[1], safe=''))
PY
)"
  dispatch="$(curl -fsS --max-time 12 -H 'Accept: application/json' "https://check-host.net/check-tcp?host=${encoded}&max_nodes=3")" || return 2
  request_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("request_id", ""))' <<<"$dispatch" 2>/dev/null || true)"
  [[ "$request_id" =~ ^[A-Za-z0-9-]+$ ]] || return 2
  for attempt in 1 2 3 4 5 6; do
    result="$(curl -fsS --max-time 12 -H 'Accept: application/json' "https://check-host.net/check-result/${request_id}" 2>/dev/null || true)"
    read -r passed complete < <(python3 - <<'PY' <<<"$result" 2>/dev/null || printf '0 0\n'
import json,sys
d=json.load(sys.stdin); p=c=0
for v in d.values():
    if v is None: continue
    c+=1
    rows=v if isinstance(v,list) else [v]
    if any(isinstance(x,dict) and 'time' in x for x in rows): p+=1
print(p,c)
PY
)
    (( passed >= 2 )) && return 0
    (( complete >= 2 )) && return 1
    sleep 2
  done
  return 2
}
