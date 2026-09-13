#!/usr/bin/env bash
set -euo pipefail

cloudflare_dns_token_file() {
  printf '%s\n' "${RHMCP_CF_DNS_TOKEN_FILE:-${CERTBOT_SECRET_DIR}/cloudflare-dns-token}"
}

save_cloudflare_dns_token() {
  local token="$1" dest="$(cloudflare_dns_token_file)"
  [[ "$token" =~ ^[A-Za-z0-9._-]{20,}$ ]] || return 1
  install -d -m 700 "$(dirname "$dest")"
  umask 077
  printf '%s\n' "$token" > "$dest"
  chmod 600 "$dest"
  record_resource cloudflare_dns_token "$dest" created
  printf '%s\n' "$dest"
}

_cf_api() {
  local method="$1" path="$2" data="${3:-}" token_file token cfg
  token_file="$(cloudflare_dns_token_file)"
  [[ -r "$token_file" ]] || return 2
  token="$(tr -d '\r\n' < "$token_file")"
  [[ "$token" =~ ^[A-Za-z0-9._-]{20,}$ ]] || { unset token; return 2; }
  install -d -m 700 "${RUNTIME_DIR:-/tmp}"
  cfg="$(mktemp "${RUNTIME_DIR:-/tmp}/cf-api.XXXXXX")"
  chmod 600 "$cfg"
  {
    printf 'silent\nshow-error\nfail-with-body\n'
    printf 'url = "https://api.cloudflare.com/client/v4%s"\n' "$path"
    printf 'request = "%s"\n' "$method"
    printf 'header = "Authorization: Bearer %s"\n' "$token"
    printf 'header = "Content-Type: application/json"\n'
  } > "$cfg"
  unset token
  if [[ -n "$data" ]]; then
    curl --config "$cfg" --data-binary "$data"
  else
    curl --config "$cfg"
  fi
  local rc=$?
  rm -f "$cfg"
  return "$rc"
}

_cf_zone_for_name() {
  local fqdn="$1" candidate response zone
  while IFS= read -r candidate; do
    response="$(_cf_api GET "/zones?name=${candidate}&status=active&per_page=1" 2>/dev/null || true)"
    zone="$(python3 -c 'import json,sys
try:
 d=json.load(sys.stdin); r=d.get("result") or []; print(r[0].get("id","") if d.get("success") and r else "")
except Exception: print("")' <<<"$response")"
    if [[ "$zone" =~ ^[A-Za-z0-9_-]{16,}$ ]]; then printf '%s\n' "$zone"; return 0; fi
  done < <(python3 - "$fqdn" <<'PY'
import sys
labels=sys.argv[1].strip('.').split('.')
for i in range(0, max(0,len(labels)-1)):
    if len(labels)-i >= 2:
        print('.'.join(labels[i:]))
PY
)
  return 1
}

_cf_create_txt() {
  local fqdn="$1" value="$2" zone payload response record
  zone="$(_cf_zone_for_name "$fqdn")" || return 1
  payload="$(python3 - "$fqdn" "$value" <<'PY'
import json,sys
print(json.dumps({"type":"TXT","name":sys.argv[1],"content":sys.argv[2],"ttl":60}))
PY
)"
  response="$(_cf_api POST "/zones/${zone}/dns_records" "$payload")" || return 1
  record="$(python3 -c 'import json,sys
try:
 d=json.load(sys.stdin); r=d.get("result") or {}; print(r.get("id","") if d.get("success") else "")
except Exception: print("")' <<<"$response")"
  [[ "$record" =~ ^[A-Za-z0-9_-]{16,}$ ]] || return 1
  printf '%s %s\n' "$zone" "$record"
}

_cf_delete_txt() {
  local fqdn="$1" value="$2" zone encoded response ids id
  zone="$(_cf_zone_for_name "$fqdn")" || return 1
  encoded="$(python3 - "$fqdn" <<'PY'
from urllib.parse import quote
import sys
print(quote(sys.argv[1], safe=''))
PY
)"
  response="$(_cf_api GET "/zones/${zone}/dns_records?type=TXT&name=${encoded}&per_page=100")" || return 1
  ids="$(python3 -c 'import json,sys
value=sys.argv[1]
try:
 d=json.load(sys.stdin)
 for r in (d.get("result") or []):
  if r.get("content")==value and r.get("id"): print(r["id"])
except Exception: pass' "$value" <<<"$response")"
  [[ -n "$ids" ]] || return 0
  while IFS= read -r id; do
    [[ "$id" =~ ^[A-Za-z0-9_-]{16,}$ ]] || continue
    _cf_api DELETE "/zones/${zone}/dns_records/${id}" >/dev/null || return 1
  done <<<"$ids"
}

_doh_txt_contains() {
  local fqdn="$1" value="$2" encoded response
  encoded="$(python3 - "$fqdn" <<'PY'
from urllib.parse import quote
import sys
print(quote(sys.argv[1], safe=''))
PY
)"
  response="$(curl -fsS --max-time 10 -H 'Accept: application/dns-json' "https://cloudflare-dns.com/dns-query?name=${encoded}&type=TXT" 2>/dev/null || true)"
  python3 -c 'import json,sys
needle=sys.argv[1]
try:
 d=json.load(sys.stdin); answers=d.get("Answer") or []
 vals=[str(a.get("data","")).strip("\"") for a in answers if a.get("type")==16]
 raise SystemExit(0 if needle in vals else 1)
except Exception: raise SystemExit(1)' "$value" <<<"$response"
}

dns_provider_create_txt() {
  local provider="$1" fqdn="$2" value="$3"
  case "$provider" in
    cloudflare) _cf_create_txt "$fqdn" "$value" ;;
    *) return 2 ;;
  esac
}

dns_provider_verify_txt() {
  local provider="$1" fqdn="$2" value="$3" timeout_s="${4:-120}" elapsed=0
  case "$provider" in cloudflare) ;; *) return 2 ;; esac
  while (( elapsed < timeout_s )); do
    if _doh_txt_contains "$fqdn" "$value"; then return 0; fi
    sleep 3; elapsed=$((elapsed+3))
  done
  return 1
}

dns_provider_delete_txt() {
  local provider="$1" fqdn="$2" value="$3"
  case "$provider" in
    cloudflare) _cf_delete_txt "$fqdn" "$value" ;;
    *) return 2 ;;
  esac
}

certbot_dns_hook() {
  local action="$1" provider="${RHMCP_DNS_PROVIDER:-cloudflare}" domain validation fqdn
  domain="${CERTBOT_DOMAIN:-}"; validation="${CERTBOT_VALIDATION:-}"
  [[ -n "$domain" && -n "$validation" ]] || return 2
  domain="${domain#\*.}"; fqdn="_acme-challenge.${domain%.}"
  case "$action" in
    auth)
      dns_provider_create_txt "$provider" "$fqdn" "$validation" >/dev/null || return 1
      if ! dns_provider_verify_txt "$provider" "$fqdn" "$validation" 120; then
        dns_provider_delete_txt "$provider" "$fqdn" "$validation" >/dev/null 2>&1 || true
        return 1
      fi
      ;;
    cleanup) dns_provider_delete_txt "$provider" "$fqdn" "$validation" >/dev/null ;;
    *) return 2 ;;
  esac
}
