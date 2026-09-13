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


def test_default_port_is_checked_not_assumed() -> None:
    install = text("installer/install.sh")
    ports = text("installer/lib/ports.sh")
    assert "choose_port 8765" in install
    assert "port_free" in ports
    assert "next_free_port" in ports


def test_cloudflare_paste_is_parsed_never_evaled() -> None:
    cf = text("installer/lib/cloudflare.sh")
    assert "service[[:space:]]+install" in cf
    assert "--token" in cf
    assert not re.search(r"(^|\s)eval(\s|$)", cf)


def test_cloudflare_download_is_version_and_checksum_pinned() -> None:
    cf = text("installer/lib/cloudflare.sh")
    assert "CLOUDFLARED_PINNED_VERSION='2026.9.1'" in cf
    assert "CLOUDFLARED_LINUX_AMD64_SHA256=" in cf
    assert "CLOUDFLARED_LINUX_ARM64_SHA256=" in cf
    assert "sha256sum" in cf
    assert "releases/latest/download" not in cf
    assert "install_pinned_cloudflared" in text("scripts/setup-cft.sh")


def test_no_noauth_installer_option() -> None:
    install = text("installer/install.sh")
    assert "AUTH_MODE=capability" in install
    assert "AUTH_MODE=oauth" in install
    assert "AUTH_MODE=none" not in install


def test_rmcp_exposes_update_rotation_and_redacted_connection_mode() -> None:
    menu = text("scripts/rmcp.sh")
    assert "check_update" in menu
    assert "rotate_key" in menu
    assert "Change language" in menu
    assert "修改语言" in menu
    assert "connection" in menu
    assert "[REDACTED]" in menu


def test_remote_bootstrap_supports_digest_and_safe_archive_paths() -> None:
    bootstrap = text("install.sh")
    assert "RHMCP_SOURCE_SHA256" in bootstrap
    assert "sha256sum" in bootstrap
    assert "Unsafe path detected in source archive" in bootstrap
    assert "--no-same-owner" in bootstrap


def test_release_banners_come_from_version_file() -> None:
    lib = text("scripts/lib.sh")
    cft = text("scripts/setup-cft.sh")
    assert "Remote Host MCP 0.1.0-alpha.1" not in lib
    assert "Remote Host MCP 0.1.0-alpha.1" not in cft
    assert "rmcp_version" in lib
    assert 'VERSION="$(tr -d' in cft


def test_direct_ingress_does_not_silently_replace_existing_proxy() -> None:
    proxy = text("installer/lib/reverse_proxy.sh")
    assert "Existing Nginx detected" in proxy
    assert "does not silently install" in proxy
    assert "nginx -t" not in proxy
