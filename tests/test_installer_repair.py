from __future__ import annotations

import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]


def run_bash(script: str, *, env: dict[str, str] | None = None, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env=merged,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def test_private_repair_requires_no_public_side_effects() -> None:
    result = run_bash(
        r"""
        source installer/lib/common.sh
        source installer/lib/lifecycle.sh
        INGRESS=private
        repair_ingress_lifecycle
        """
    )
    assert result.returncode == 0, result.stderr
    assert "no public ingress repair required" in result.stdout


def test_tunnel_repair_requires_existing_secret_and_external_health(tmp_path: pathlib.Path) -> None:
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    token = secret_dir / "cloudflared.token"
    token.write_text("placeholder-token-not-real\n", encoding="utf-8")
    marker = tmp_path / "service-called"

    result = run_bash(
        f"""
        source installer/lib/common.sh
        source installer/lib/lifecycle.sh
        INGRESS=cloudflare-tunnel
        SECRET_DIR={secret_dir!s}
        PUBLIC_HOST=example.invalid
        install_managed_tunnel_service() {{ test "$1" = {token!s}; touch {marker!s}; }}
        external_http_probe() {{ return 42; }}
        if repair_ingress_lifecycle; then rc=0; else rc=$?; fi
        test "$rc" -eq 1
        test -f {marker!s}
        """
    )
    assert result.returncode == 0, result.stderr

    token.unlink()
    missing = run_bash(
        f"""
        source installer/lib/common.sh
        source installer/lib/lifecycle.sh
        INGRESS=cloudflare-tunnel
        SECRET_DIR={secret_dir!s}
        PUBLIC_HOST=example.invalid
        repair_ingress_lifecycle
        """
    )
    assert missing.returncode != 0
    assert "will not reconstruct secrets" in missing.stderr


def test_domain_https_uses_dns01_only_when_http01_unavailable_and_provider_ready(tmp_path: pathlib.Path) -> None:
    marker = tmp_path / "dns01"
    result = run_bash(
        f"""
        source installer/lib/common.sh
        source installer/lib/tls.sh
        LOCAL_PORT=8765
        PUBLIC_HTTPS_PORT=443
        ACME_WEBROOT={tmp_path!s}/webroot
        ensure_nginx_for_direct() {{ :; }}
        ensure_product_certbot() {{ mkdir -p "$ACME_WEBROOT"; }}
        write_managed_nginx_http() {{ :; }}
        acme_external_http_preflight() {{ return 42; }}
        _cloudflare_dns01_available_or_prompt() {{ return 0; }}
        issue_domain_http01() {{ return 99; }}
        issue_domain_dns01() {{ touch {marker!s}; }}
        set_certificate_paths() {{ :; }}
        write_managed_nginx_https() {{ :; }}
        renewal_dry_run_gate() {{ :; }}
        setup_renewal_timer() {{ :; }}
        verify_external_https() {{ :; }}
        configure_domain_https example.com ops@example.com cloudflare-dns-only 443
        test -f {marker!s}
        """
    )
    assert result.returncode == 0, result.stderr


def test_domain_https_fails_closed_when_http01_and_dns01_are_unavailable(tmp_path: pathlib.Path) -> None:
    result = run_bash(
        f"""
        source installer/lib/common.sh
        source installer/lib/tls.sh
        LOCAL_PORT=8765
        PUBLIC_HTTPS_PORT=443
        ACME_WEBROOT={tmp_path!s}/webroot
        ensure_nginx_for_direct() {{ :; }}
        ensure_product_certbot() {{ mkdir -p "$ACME_WEBROOT"; }}
        write_managed_nginx_http() {{ :; }}
        acme_external_http_preflight() {{ return 42; }}
        _cloudflare_dns01_available_or_prompt() {{ return 1; }}
        if configure_domain_https example.com ops@example.com other 443; then rc=0; else rc=$?; fi
        test "$rc" -eq 1
        """
    )
    assert result.returncode == 0, result.stderr


def test_public_ip_https_never_falls_back_to_dns01(tmp_path: pathlib.Path) -> None:
    marker = tmp_path / "dns-called"
    result = run_bash(
        f"""
        source installer/lib/common.sh
        source installer/lib/tls.sh
        LOCAL_PORT=8765
        PUBLIC_HTTPS_PORT=443
        ACME_WEBROOT={tmp_path!s}/webroot
        ensure_nginx_for_direct() {{ :; }}
        ensure_product_certbot() {{ mkdir -p "$ACME_WEBROOT"; }}
        write_managed_nginx_http() {{ :; }}
        acme_external_http_preflight() {{ return 42; }}
        issue_domain_dns01() {{ touch {marker!s}; }}
        issue_ip_http01() {{ return 88; }}
        if configure_public_ip_https 203.0.113.10 ops@example.com 443; then rc=0; else rc=$?; fi
        test "$rc" -eq 1
        test ! -e {marker!s}
        """
    )
    assert result.returncode == 0, result.stderr


def test_dns01_only_domain_path_skips_http01_and_keeps_public_local_ports_separate(tmp_path: pathlib.Path) -> None:
    dns_marker = tmp_path / "dns01"
    https_marker = tmp_path / "https"
    bad_marker = tmp_path / "unexpected-http"
    result = run_bash(
        f"""
        source installer/lib/common.sh
        source installer/lib/tls.sh
        source installer/lib/port_hardening.sh
        LOCAL_PORT=8765
        PUBLIC_HTTPS_PORT=2012
        HTTPS_LISTEN_PORT=443
        DOMAIN_CHALLENGE_MODE=dns-01
        ACME_WEBROOT={tmp_path!s}/webroot
        ensure_nginx_for_direct() {{ test "$1" = false; test "$2" = 443; }}
        ensure_product_certbot() {{ mkdir -p "$ACME_WEBROOT"; }}
        ensure_managed_nginx_config_marker() {{ :; }}
        _cloudflare_dns01_available_or_prompt() {{ return 0; }}
        write_managed_nginx_http() {{ touch {bad_marker!s}; return 90; }}
        acme_external_http_preflight() {{ touch {bad_marker!s}; return 91; }}
        issue_domain_http01() {{ touch {bad_marker!s}; return 92; }}
        issue_domain_dns01() {{ touch {dns_marker!s}; CERT_RENEWAL_MODE=dns-01-cloudflare; }}
        set_certificate_paths() {{ CERT_FULLCHAIN=/tmp/fake-fullchain; CERT_PRIVKEY=/tmp/fake-privkey; }}
        write_managed_nginx_https() {{ test "$6" = 443; test "$7" = 2012; test "$8" = false; touch {https_marker!s}; }}
        renewal_dry_run_gate() {{ :; }}
        setup_renewal_timer() {{ :; }}
        verify_external_https() {{ test "$1" = example.com; test "$2" = 2012; }}
        configure_domain_https example.com ops@example.com cloudflare-dns-only 443 2012 dns-01
        test -f {dns_marker!s}
        test -f {https_marker!s}
        test ! -e {bad_marker!s}
        """
    )
    assert result.returncode == 0, result.stderr


def test_nginx_https_writer_supports_public_to_local_port_mapping_without_http_listener(tmp_path: pathlib.Path) -> None:
    config = tmp_path / "remote-host-mcp.conf"
    cert = tmp_path / "fullchain.pem"
    key = tmp_path / "privkey.pem"
    webroot = tmp_path / "webroot"
    config.write_text("# Managed-By: remote-host-mcp\n", encoding="utf-8")
    cert.write_text("dummy\n", encoding="utf-8")
    key.write_text("dummy\n", encoding="utf-8")
    result = run_bash(
        f"""
        source installer/lib/common.sh
        source installer/lib/lifecycle.sh
        source installer/lib/reverse_proxy.sh
        RHMCP_NGINX_CONFIG_PATH={config!s}
        reload_nginx_safely() {{ :; }}
        write_managed_nginx_https example.com 8765 {webroot!s} {cert!s} {key!s} 443 2012 false
        grep -q '^    listen 443 ssl;$' {config!s}
        ! grep -q '^    listen 80;$' {config!s}
        grep -q 'proxy_pass http://127.0.0.1:8765;' {config!s}
        """
    )
    assert result.returncode == 0, result.stderr


def test_portable_certificate_renewal_is_identity_tracked_stoppable_and_reaps_child(tmp_path: pathlib.Path) -> None:
    config = tmp_path / "config"
    state = tmp_path / "state"
    logs = tmp_path / "logs"
    certbot_dir = config / "certbot"
    ownership = config / "ownership.env"
    config.mkdir()
    state.mkdir()
    logs.mkdir()
    certbot_dir.mkdir()
    env_file = config / "rhmcp.env"
    env_file.write_text("RHMCP_AUTH_MODE=capability\n", encoding="utf-8")
    renew = certbot_dir / "renew.sh"
    renew.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    renew.chmod(0o700)

    result = run_bash(
        f"""
        source installer/lib/common.sh
        source installer/lib/state.sh
        source installer/lib/lifecycle.sh
        source installer/lib/port_hardening.sh
        CONFIG_DIR={config!s}
        STATE_DIR={state!s}
        LOG_DIR={logs!s}
        CERTBOT_DIR={certbot_dir!s}
        OWNERSHIP_STATE={ownership!s}
        CERTBOT_RENEW_HOOK={renew!s}
        RHMCP_CERT_RENEW_INTERVAL_S=60
        systemd_operational() {{ return 1; }}
        setup_renewal_timer
        pidfile={logs!s}/cert-renew.pid
        loop={certbot_dir!s}/renew-loop.sh
        portable_renewal_alive "$pidfile" "$loop"
        test "$(resource_value portable_cert_renew_pid OWNERSHIP)" = created
        test "$(resource_value cert_renew_loop OWNERSHIP)" = created
        grep -q '^RHMCP_CERT_RENEW_BACKEND=portable$' {env_file!s}

        parent_pid="$(cat "$pidfile")"
        child_pid=''
        for _ in {{1..40}}; do
          child_pid="$(cat "/proc/$parent_pid/task/$parent_pid/children" 2>/dev/null | awk '{{print $1}}')"
          [[ "$child_pid" =~ ^[0-9]+$ ]] && break
          sleep 0.05
        done
        [[ "$child_pid" =~ ^[0-9]+$ ]]
        child_ticks="$(_portable_proc_start_ticks "$child_pid")"
        [[ "$child_ticks" =~ ^[0-9]+$ ]]

        stop_portable_renewal "$pidfile" "$loop"
        test ! -e "$pidfile"
        test ! -e "${{pidfile}}.start_ticks"
        ! portable_renewal_alive "$pidfile" "$loop"
        for _ in {{1..40}}; do
          if ! kill -0 "$child_pid" 2>/dev/null; then break; fi
          current_ticks="$(_portable_proc_start_ticks "$child_pid" 2>/dev/null || true)"
          [[ "$current_ticks" != "$child_ticks" ]] && break
          sleep 0.05
        done
        if kill -0 "$child_pid" 2>/dev/null; then
          current_ticks="$(_portable_proc_start_ticks "$child_pid" 2>/dev/null || true)"
          test "$current_ticks" != "$child_ticks"
        fi
        """
    )
    assert result.returncode == 0, result.stderr


def test_port_hardening_and_configure_contract_is_present() -> None:
    install = (ROOT / "installer/install.sh").read_text(encoding="utf-8")
    hardening = (ROOT / "installer/lib/port_hardening.sh").read_text(encoding="utf-8")
    configure = (ROOT / "installer/lib/configure.sh").read_text(encoding="utf-8")
    rmcp = (ROOT / "scripts/rmcp.sh").read_text(encoding="utf-8")
    uninstall = (ROOT / "installer/lib/uninstall.sh").read_text(encoding="utf-8")

    assert "RHMCP_HTTPS_LISTEN_PORT" in install
    assert "RHMCP_DOMAIN_CHALLENGE_MODE" in install
    assert "DNS-01 only (no public port 80 required)" in install
    assert "--no-cache-dir" in install
    assert "--no-cache-dir" in hardening
    assert "write_managed_nginx_https" in hardening
    assert "setup_portable_renewal" in hardening
    assert "portable_renewal_alive" in hardening
    assert "RHMCP_CERT_RENEW_BACKEND" in hardening
    assert "portable_cert_renew_pid" in uninstall
    assert "cert_renew_loop" in uninstall
    assert "apply_https_port_mapping" in configure
    assert "_config_restore_snapshot" in configure
    assert "configure" in rmcp
    assert "Configuration (ports/mapping)" in rmcp
