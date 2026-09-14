#!/usr/bin/env bash
set -euo pipefail

detect_domain_dns_mode() {
  local host="$1" token_file zone encoded response mode
  token_file="$(cloudflare_dns_token_file 2>/dev/null || true)"
  [[ -n "$token_file" && -r "$token_file" ]] || { printf 'unknown\n'; return 0; }
  zone="$(_cf_zone_for_name "$host" 2>/dev/null || true)"
  [[ -n "$zone" ]] || { printf 'unknown\n'; return 0; }
  encoded="$(python3 - "$host" <<'PY'
from urllib.parse import quote
import sys
print(quote(sys.argv[1], safe=''))
PY
)"
  response="$(_cf_api GET "/zones/${zone}/dns_records?name=${encoded}&per_page=100" 2>/dev/null || true)"
  mode="$(python3 -c 'import json,sys
try:
 d=json.load(sys.stdin); r=[x for x in (d.get("result") or []) if x.get("type") in {"A","AAAA","CNAME"}]
 if not r: print("unknown")
 elif any(bool(x.get("proxied")) for x in r): print("cloudflare-proxied")
 else: print("cloudflare-dns-only")
except Exception: print("unknown")' <<<"$response")"
  printf '%s\n' "$mode"
}
