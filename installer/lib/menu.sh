#!/usr/bin/env bash
set -euo pipefail

# Pre-install entry menu (full-colour, numbered panels).
#
# This module is a thin dispatcher and deliberately owns no lifecycle logic:
#   - options 1/2 hand control straight back to the normal installer pipeline.
#     Option 2 only pre-seeds RHMCP_INGRESS_PROFILE, which choose_ingress()
#     already honours through ni_prompt()'s environment short-circuit;
#   - options 3-6 delegate to the installed `rmcp` lifecycle CLI
#     (scripts/rmcp.sh exposes the full post-install surface already).
#
# Reachability is intentionally narrow: a real TTY on both stdin and stdout,
# plus no CLI argument at all. CI drives installer/install.sh through a fixed
# piped stdin sequence, so the non-interactive contract must stay byte-exact.

RHMCP_MENU_DISABLED="${RHMCP_MENU_DISABLED:-0}"

menu_rmcp_bin() { command -v rmcp 2>/dev/null || true; }

menu_banner() {
  local tagline system version
  tagline="$(t menu_tagline)"
  system="$(uname -srm 2>/dev/null || printf 'unknown')"
  version="${RMCP_VERSION:-unknown}"
  hr
  printf ' %s\n' "$(paint "$C_CYAN" "$(t menu_title)")"
  printf ' %s\n' "$tagline"
  subhr
  printf ' %s : %s\n' "$(t menu_system)" "$system"
  printf ' %s : %s\n' "$(t menu_version)" "$version"
  hr
}

menu_pause() {
  printf '\n'
  read -r -p "$(t press_enter)" _ || true
}

# Delegate one lifecycle action to the installed launcher. When the product is
# not installed yet the option fails closed with a pointer at option 1 instead
# of silently doing nothing.
menu_run_rmcp() {
  local bin
  bin="$(menu_rmcp_bin)"
  if [[ -z "$bin" ]]; then
    warn "$(t menu_not_installed)"
    return 1
  fi
  "$bin" "$@"
}

menu_service_panel() {
  local choice
  while true; do
    clear 2>/dev/null || true
    hr
    printf ' %s\n' "$(t menu_service)"
    hr
    printf '  1. %s\n' "$(t menu_svc_status)"
    printf '  2. %s\n' "$(t menu_svc_restart)"
    printf '  3. %s\n' "$(t menu_svc_configure)"
    printf '  0. %s\n' "$(t menu_back)"
    hr
    read -r -p "$(t menu_select) [0-3]: " choice || choice=0
    case "$choice" in
      1) menu_run_rmcp status || true; menu_pause ;;
      2) menu_run_rmcp restart || true; menu_pause ;;
      3) menu_run_rmcp configure || true; menu_pause ;;
      0) return 0 ;;
      *) warn "$(t menu_invalid)" ;;
    esac
  done
}

menu_main() {
  local choice
  while true; do
    clear 2>/dev/null || true
    menu_banner
    if [[ -n "$(menu_rmcp_bin)" ]]; then
      ok "$(t menu_installed_hint)"
    fi
    printf '  1. %s\n' "$(t menu_install)"
    printf '  2. %s\n' "$(t menu_tunnel)"
    printf '  3. %s\n' "$(t menu_status)"
    printf '  4. %s\n' "$(t menu_service)"
    printf '  5. %s\n' "$(t menu_repair)"
    printf '  6. %s\n' "$(t menu_uninstall)"
    printf '  0. %s\n' "$(t menu_exit)"
    hr
    read -r -p "$(t menu_select) [0-6]: " choice || choice=0
    case "$choice" in
      1) return 0 ;;
      2) RHMCP_INGRESS_PROFILE=3; export RHMCP_INGRESS_PROFILE; return 0 ;;
      3) menu_run_rmcp status || true; menu_run_rmcp connection || true; menu_pause ;;
      4) menu_service_panel ;;
      5) menu_run_rmcp doctor || true; menu_pause ;;
      6) menu_run_rmcp uninstall || true; menu_pause ;;
      0|q|Q) exit 0 ;;
      *) warn "$(t menu_invalid)" ;;
    esac
  done
}

# Entry point. Returns with the caller's normal pipeline untouched, so every
# option either exits outright or leaves the installer to run its usual flow.
maybe_show_start_menu() {
  [[ "$RHMCP_MENU_DISABLED" != 1 ]] || return 0
  [[ -t 0 && -t 1 ]] || return 0
  [[ -z "${RHMCP_NO_MENU:-}" ]] || return 0
  # The panel is Chinese-first by default. RHMCP_LANGUAGE is deliberately NOT
  # forced: setting it would short-circuit select_language() right below and
  # silently remove the English path for interactive operators. An explicit
  # RHMCP_LANGUAGE still wins, which keeps RHMCP_LANGUAGE=en_US automation intact.
  load_locale "${RHMCP_LANGUAGE:-zh_CN}"
  menu_main
}
