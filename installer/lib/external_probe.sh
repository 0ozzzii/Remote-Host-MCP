#!/usr/bin/env bash
set -euo pipefail

EXTERNAL_PROBE_LAST=''
EXTERNAL_PROBE_BLOCK_HINT=''

_provider_block_fingerprint() {
  local text="$1"
  grep -Eqi 'server[" :]+beaver|non-compliance[[:space:]]+icp[[:space:]]+filing|beian-block|icp[[:space:]_-]*filing' <<<"$text"
}

_urlencode() {
  python3 - "$1" <<'PY'
from urllib.parse import quote
import sys
print(quote(sys.argv[1], safe=''))
PY
}

_parse_custom_http_result() {
  local expected="$1"
  python3 -c 'import json,sys
expected=int(sys.argv[1])
try:
    x=json.load(sys.stdin)
    status=x.get("status") or x.get("status_code") or x.get("http_status")
    reachable=x.get("reachable", True)
    print("pass" if reachable and int(status)==expected else ("blocked" if int(status or 0)==403 else "fail"))
except Exception:
    raise SystemExit(1)' "$expected"
}

_parse_checkhost_http_result() {
  local expected="$1"
  python3 -c 'import json,sys
expected=str(int(sys.argv[1])); data=json.load(sys.stdin); statuses=[]
for value in data.values():
    if not value: continue
    rows=value if isinstance(value,list) else [value]
    for row in rows:
        if isinstance(row,list) and row and isinstance(row[0],list): row=row[0]
        if isinstance(row,list) and len(row)>=4 and row[3] is not None: statuses.append(str(row[3]))
passed=sum(s==expected for s in statuses); blocked=sum(s=="403" for s in statuses)
print(f"{passed}:{blocked}:{len(statuses)}")' "$expected"
}

_parse_checkhost_tcp_result() {
  python3 -c 'import json,sys
d=json.load(sys.stdin); p=c=0
for v in d.values():
    if v is None: continue
    c+=1; rows=v if isinstance(v,list) else [v]
    if any(isinstance(x,dict) and "time" in x for x in rows): p+=1
print(p,c)'
}

_checkhost_dispatch() {
  local target="$1" encoded
  encoded="$(_urlencode "$target")"
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
    encoded="$(_urlencode "$target")"
    if [[ "$RHMCP_EXTERNAL_PROBE_URL" == *'{url}'* ]]; then
      endpoint="${RHMCP_EXTERNAL_PROBE_URL//\{url\}/$encoded}"
    elif [[ "$RHMCP_EXTERNAL_PROBE_URL" == *\?* ]]; then
      endpoint="${RHMCP_EXTERNAL_PROBE_URL}&url=${encoded}"
    else
      endpoint="${RHMCP_EXTERNAL_PROBE_URL}?url=${encoded}"
    fi
    result="$(curl -fsS --max-time 15 "$endpoint")" || return 2
    EXTERNAL_PROBE_LAST="$result"
    if _provider_block_fingerprint "$result"; then EXTERNAL_PROBE_BLOCK_HINT='PROVIDER_POLICY_BLOCK_FINGERPRINT'; return 42; fi
    parsed="$(printf '%s' "$result" | _parse_custom_http_result "$expected" 2>/dev/null || true)"
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
    parsed="$(printf '%s' "$result" | _parse_checkhost_http_result "$expected" 2>/dev/null || true)"
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
  local host="$1" port="$2" encoded dispatch request_id result attempt parsed
  encoded="$(_urlencode "${host}:${port}")"
  dispatch="$(curl -fsS --max-time 12 -H 'Accept: application/json' "https://check-host.net/check-tcp?host=${encoded}&max_nodes=3")" || return 2
  request_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("request_id", ""))' <<<"$dispatch" 2>/dev/null || true)"
  [[ "$request_id" =~ ^[A-Za-z0-9-]+$ ]] || return 2
  for attempt in 1 2 3 4 5 6; do
    result="$(curl -fsS --max-time 12 -H 'Accept: application/json' "https://check-host.net/check-result/${request_id}" 2>/dev/null || true)"
    parsed="$(printf '%s' "$result" | _parse_checkhost_tcp_result 2>/dev/null || printf '0 0')"
    if [[ "$parsed" =~ ^([0-9]+)[[:space:]]+([0-9]+)$ ]]; then
      (( BASH_REMATCH[1] >= 2 )) && return 0
      (( BASH_REMATCH[2] >= 2 )) && return 1
    fi
    sleep 2
  done
  return 2
}
