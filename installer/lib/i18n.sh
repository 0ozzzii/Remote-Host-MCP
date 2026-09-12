#!/usr/bin/env bash
set -euo pipefail

declare -Ag MSG=()

load_locale() {
  local lang="${1:-en_US}" base
  base="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  MSG=()
  case "$lang" in
    zh_CN|zh|cn) lang=zh_CN ;;
    en_US|en) lang=en_US ;;
    *) lang=en_US ;;
  esac
  # shellcheck disable=SC1090
  source "$base/locales/${lang}.sh"
  RMCP_LANGUAGE="$lang"
  export RMCP_LANGUAGE
}

t() {
  local key="$1"
  printf '%s' "${MSG[$key]:-$key}"
}
