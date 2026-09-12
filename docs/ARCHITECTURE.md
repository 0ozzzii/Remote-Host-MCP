# Remote Host MCP 0.1.0-alpha.1 — Architecture

## Product boundary

Remote Host MCP is a general-purpose MCP control plane for one Linux host or container boundary. It is not tied to ModelScope DSW; DSWD is the ModelScope deployment profile.

The authority boundary is the OS identity running RHMCP. Dedicated filesystem/transfer tools add an allowed-root boundary, but arbitrary shell/PTY/job execution retains normal OS permissions.

## Canonical 3.0 identity

- distribution: `remote-host-mcp`
- Python package: `remote_host_mcp`
- CLI: `remote-host-mcp`
- environment: `RHMCP_*`
- protocol: MCP Streamable HTTP
- serverInfo/health identity: `Remote Host MCP`

Compatibility is deliberate:

- `dsw-direct-mcp` CLI alias;
- `dsw_direct_mcp.*` namespace;
- `DSW_MCP_*` environment files;
- old DSWD 2.x branch/release remains rollback source.

## Layering

```text
remote_host_mcp.main
        │
        ▼
remote_host_mcp.app             canonical generic runtime
        │
        ├── compat_env           RHMCP_* → legacy implementation namespace
        ├── branding             generic server/tool/health metadata adapter
        │
        ▼
remote_host_mcp.host_server     proven 2.x execution composition
        │
        ├── base shell/files/transfer server
        ├── durable jobs + MCP Tasks
        ├── PTY
        ├── process/service/system
        └── OAuth resource server
```

The branding adapter intentionally does not change tool names, schemas, handlers or execution semantics. It only changes product metadata and legacy DSW-specific model-visible copy. This keeps the 3.0 generalization orthogonal to the already-validated execution core.

Because MCP SDK 2.2.0 currently lacks a public registered-tool metadata update API, the branding adapter uses the pinned SDK's internal `_tool_manager`, `_lowlevel_server` and custom-route collection. The SDK version is pinned and CI treats this adapter as a mandatory compatibility gate; an SDK upgrade cannot be accepted without revalidating this seam.

## Execution domains

### Sync Exec

`exec` is a one-shot arbitrary shell command with a hard 90-second effective maximum, bounded output and process-group cleanup. The server never automatically retries arbitrary shell.

### Filesystem and Transfer

Dedicated file operations enforce `RHMCP_ALLOWED_ROOTS` using openat2 when supported and stable dirfd/`*at` operations for mutations. Resumable transfer uses explicit handles, offsets, size/SHA-256 validation and atomic commit.

### Durable Jobs / MCP Tasks

Long-running work uses persistent job metadata, heartbeat, worker/child identity and cursor-addressable logs. Restart recovery observes/reconciles and never replays shell. MCP Tasks reuses the same job_id as taskId.

### Persistent PTY

PTY sessions support real interactive shell state, ANSI/TUI screen, resize, raw writes and signals. OSC 133 gives exact normal completion; foreground process-group state is the fallback for interrupted flows. PTYs survive transport reconnect, not RHMCP process restart.

### Process / Service / System

Processes use PID+start_ticks identity; online signals use pidfd when available. Service operations use validated systemctl argv without shell expansion. System information is bounded and avoids environment-secret dumping.

## Authentication

Capability mode is the default private single-owner model. OAuth 2.1 Resource Server mode is implemented but optional/default OFF. RHMCP validates bearer signature, issuer, audience/resource, expiry and scopes; an external OAuth/OIDC Authorization Server owns login, PKCE, refresh tokens and client metadata/registration.

## Connectivity

RHMCP binds to loopback by design. Public ingress belongs to an HTTPS reverse proxy/private tunnel such as Cloudflare Tunnel. In the ModelScope DSW profile the existing tunnel remains separate from the application release so application rollback does not require a network rollback.

## Deployment profiles

- Generic Linux: VPS/VM/workstation.
- ModelScope DSW: `/mnt/workspace` persistence, DSWC-managed blue/green cutover, existing DSWD rollback.
- Docker/container: container namespace is the host boundary unless explicit host namespaces/mounts are exposed.

## 3.0 release principle

The 3.0 major version represents product generalization and breaking public naming changes, not a rewrite of the execution engine. Existing 2.1 hardening—MCP Tasks, OSC 133, pidfd, openat2/dirfd and optional OAuth—remains the runtime foundation.
