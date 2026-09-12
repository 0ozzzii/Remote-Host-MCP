# Remote Host MCP

Remote Host MCP is a general-purpose MCP server for AI-controlled remote Linux hosts and containers. It targets SSH/SCP-class host administration workflows while adding structured MCP tools, durable jobs, MCP Tasks, persistent PTY sessions, process/service control, hardened filesystem access, and resumable transfers.

## Current release line

**Version: `0.1.0-alpha.1`**

This is the first independent-product alpha. The implementation descends from the validated DSWD 2.x / internal Remote Host MCP 3.0 development line, but the standalone product version series intentionally restarts at `0.x` until its public API, deployment model, and multi-host compatibility are proven on real DSW/VPS/container environments.

Current state: **pre-production / real-host canary candidate**. Repository CI is required before deployment; real-host validation is required before any production-ready claim.

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

The ModelScope DSW profile is one deployment of the generic product; DSW-specific state is not the product's code source of truth.

## Source-of-truth policy

This repository is the code/version/CI source of truth for Remote Host MCP from `0.1.0-alpha.1` onward. Historical development provenance and hard rollback evidence remain preserved in the private repository `0ozzzii/cf-remote-mcp`, especially branch `remote-host-mcp-v3`; that branch is intentionally left intact.

Real runtime state is always determined on the target host itself. For ModelScope DSW that means `/mnt/workspace` plus the active DSWC/DSWD control path.

## Versioning

Remote Host MCP follows Semantic Versioning. While the product is pre-1.0, `0.x` releases may still change public interfaces. Alpha/beta prereleases are used for real-host validation before a stable release.

## Security

Never commit tokens, API keys, tunnel credentials, SSH private keys, MCP capability paths, connection strings, or production `.env` files. Example configuration files contain placeholders only.

## Historical compatibility

The canonical interface is `remote-host-mcp`, `remote_host_mcp.*`, and `RHMCP_*`. The legacy `dsw-direct-mcp`, `dsw_direct_mcp.*`, and `DSW_MCP_*` surfaces remain only to make the first DSWD migration reversible.
