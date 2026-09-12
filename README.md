# Remote Host MCP

Remote Host MCP is a general-purpose MCP server for AI-controlled remote Linux hosts and containers. It targets SSH/SCP-class host administration workflows while adding structured MCP tools, durable jobs, MCP Tasks, persistent PTY sessions, process/service control, hardened filesystem access, and resumable transfers.

## Current development line

**Version: `0.2.0-alpha.1`** (`0.2.0a1` in Python packaging)

The validated `0.1.0-alpha.1` runtime established the independent repository and 43-tool MCP baseline. The `0.2.0-alpha.1` line productizes installation and lifecycle management without redesigning that runtime surface.

## Installer goals

The new installer is bilingual (简体中文 / English) and separates two primary ingress paths:

- **Public IP / domain** — for VPS/cloud hosts. Remote Host MCP remains loopback-only behind HTTPS reverse proxying.
- **Cloudflare Tunnel** — for ModelScope DSW, NAT, home servers and hosts without public ingress.

Port `8765` is preferred but checked. If occupied, the installer offers the next free port or a custom port and never kills the existing owner. ModelScope DSW-like hosts can use a persistent-prefix layout such as `/mnt/workspace/remote-host-mcp`; standard VPS installs use `/opt`, `/etc`, `/var/lib` and `/var/log` layouts.

Run from a checked-out repository:

```bash
bash install.sh
```

The public-repository bootstrap also supports a curl-fed entry once the target release/ref is selected:

```bash
curl -fsSL https://raw.githubusercontent.com/0ozzzii/Remote-Host-MCP/main/install.sh | bash
```

For prerelease testing, pin `RHMCP_INSTALL_REF` to the intended branch/commit instead of assuming a moving `main`.

After installation, use one permanent management command:

```bash
rmcp
```

It provides service control, connection information, capability-key rotation, diagnostics, update checking and language selection.

See `docs/INSTALLER_ARCHITECTURE.md` for design and safety boundaries.

## Core capabilities

- bounded shell execution
- persistent PTY / terminal sessions
- durable Jobs and MCP Tasks
- filesystem tools with hardened path handling
- chunked/resumable upload and download primitives
- process inspection and signaling
- service/system inspection and control primitives
- capability-mode authentication, plus optional OAuth 2.1 Resource Server mode
- compatibility layer for existing DSWD deployments during migration

## Deployment profiles

- `deployments/generic-linux/`
- `deployments/modelscope-dsw/`
- `deployments/docker/`

## Security

Remote Host MCP itself stays bound to loopback. Public access should be provided by HTTPS reverse proxy or Cloudflare Tunnel. Capability URL is the default authentication mode; OAuth 2.1 is available for advanced deployments. There is no unauthenticated public installer mode.

Never commit tokens, API keys, tunnel credentials, SSH private keys, MCP capability paths, connection strings, or production `.env` files.

## Historical compatibility

The canonical interface is `remote-host-mcp`, `remote_host_mcp.*`, and `RHMCP_*`. The legacy `dsw-direct-mcp`, `dsw_direct_mcp.*`, and `DSW_MCP_*` surfaces remain only for reversible migration of existing DSWD deployments.
