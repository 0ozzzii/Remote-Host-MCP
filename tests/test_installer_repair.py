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
