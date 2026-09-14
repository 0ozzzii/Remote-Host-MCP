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
def walk(x):
    if isinstance(x, list):
        if len(x) >= 4 and isinstance(x[3], (int,str)) and str(x[3]).isdigit():
            statuses.append(str(x[3])); return
        for item in x: walk(item)
    elif isinstance(x, dict):
        for item in x.values(): walk(item)
for value in data.values():
    if value is not None: walk(value)
passed=sum(s==expected for s in statuses); blocked=sum(s=="403" for s in statuses)
print(f"{passed}:{blocked}:{len(statuses)}")' "$expected"
}

_parse_checkhost_tcp_result() {
  python3 -c 'import json,sys
d=json.load(sys.stdin); p=c=0
def has_success(x):
    if isinstance(x, dict):
        if "time" in x and x.get("error") in (None, ""): return True
        return any(has_success(v) for v in x.values())
    if isinstance(x, list): return any(has_success(v) for v in x)
    return False
for v in d.values():
    if v is None: continue
    c+=1
    if has_success(v): p+=1
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
  [[ "$request_id" =~ ^[A-Za-z0-9._-]+$ ]] || return 2
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
  [[ "$request_id" =~ ^[A-Za-z0-9._-]+$ ]] || return 2
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
