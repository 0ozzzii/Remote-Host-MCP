#!/usr/bin/env bash
set -euo pipefail
python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
