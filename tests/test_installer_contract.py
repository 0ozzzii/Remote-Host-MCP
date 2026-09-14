from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_installer_has_bilingual_locales() -> None:
    assert (ROOT / "installer/locales/zh_CN.sh").is_file()
    assert (ROOT / "installer/locales/en_US.sh").is_file()
    zh = text("installer/locales/zh_CN.sh")
    en = text("installer/locales/en_US.sh")
    assert "请选择安装语言" in zh
    assert "Remote Host MCP Installer" in en


def test_install_modes_cover_system_and_persistent_prefix() -> None:
    paths = text("installer/lib/paths.sh")
    assert "/opt/remote-host-mcp" in paths
    assert "/mnt/workspace/remote-host-mcp" in paths
    assert "INSTALL_MODE=system" in paths
    assert "INSTALL_MODE=prefix" in paths
    assert "resource-ownership.env" in paths
    assert "install-progress.env" in paths


def test_default_port_is_checked_not_assumed() -> None:
    install = text("installer/install.sh")
    ports = text("installer/lib/ports.sh")
    assert "choose_port 8765" in install
    assert "port_free" in ports
    assert "next_free_port" in ports
    assert "RHMCP_BIND_HOST=127.0.0.1" in install


def test_cloudflare_paste_is_parsed_never_evaled() -> None:
    cf = text("installer/lib/cloudflare.sh")
    assert "service[[:space:]]+install" in cf
    assert "--token" in cf
    assert "token-file" in cf
    assert not re.search(r"(^|\s)eval(\s|$)", cf)


def test_no_noauth_installer_option() -> None:
    install = text("installer/install.sh")
    assert "AUTH_MODE=capability" in install
    assert "AUTH_MODE=oauth" in install
    assert "AUTH_MODE=none" not in install


def test_rmcp_exposes_product_lifecycle_commands() -> None:
    menu = text("scripts/rmcp.sh")
    for command in (
        "status",
        "doctor",
        "logs",
        "restart",
        "repair",
        "resume",
        "uninstall",
        "stray-check",
        "check-update",
        "connection",
    ):
        assert command in menu
    assert "uninstall --dry-run" in menu
    assert "uninstall --purge" in menu


def test_direct_ingress_refuses_foreign_proxy_ownership() -> None:
    proxy = text("installer/lib/reverse_proxy.sh")
    tls = text("installer/lib/tls.sh")
    assert "Managed-By: remote-host-mcp" in proxy
    assert "Refusing automatic takeover" in proxy
    assert "Foreign Nginx config occupies product path" in proxy
    assert "nginx -t" in proxy
    assert "Active ${proxy} detected" in tls


def test_https_profiles_are_explicit_and_loopback_backend_is_preserved() -> None:
    install = text("installer/install.sh")
    assert "Public IP HTTPS" in install
    assert "Domain HTTPS" in install
    assert "Cloudflare Tunnel" in install
    assert "Private/local only" in install
    assert "PUBLIC_HTTPS_PORT='443'" in install
    assert "RHMCP_BIND_HOST=127.0.0.1" in install


def test_build_provenance_is_resolved_before_archive_copy() -> None:
    bootstrap = text("install.sh")
    state = text("installer/lib/state.sh")
    assert "RHMCP_RESOLVED_COMMIT" in bootstrap
    assert "RHMCP_REQUESTED_REF" in bootstrap
    assert "RHMCP_BUILD_COMMIT" in state
    assert "RHMCP_BUILD_REF" in state
