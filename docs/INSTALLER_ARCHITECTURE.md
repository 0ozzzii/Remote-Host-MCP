# Remote Host MCP Installer Architecture — 0.2.0-alpha.1

## Goal

Turn Remote Host MCP from a repository-oriented two-step bootstrap into a reusable bilingual Linux installer and persistent `rmcp` management entrypoint, without rewriting the validated 43-tool runtime.

## Product decisions

1. First screen: Simplified Chinese or English. The selected language controls the rest of the installer and can later be changed from `rmcp`.
2. Two primary ingress models: public IP/domain, or Cloudflare Tunnel for DSW/NAT/no-public-IP environments.
3. Port 8765 is preferred, not assumed. If occupied, the installer shows the owner when possible and asks for the next free or a custom port. It never kills an unknown process.
4. Authentication is capability URL by default or OAuth 2.1 advanced mode. There is no unauthenticated Internet mode.
5. Full-host control is an explicit root profile. Current-user control remains available. `RHMCP_ALLOWED_ROOTS` does not sandbox exec/job/terminal; the OS service identity is the real shell authority.
6. Normal VPS deployments use a versioned system layout. DSW/container/special environments can use one persistent prefix. `/mnt/workspace` is detected and recommended for ModelScope DSW-like environments.
7. Existing reverse proxies win. Nginx/Caddy/Apache are detected and never silently replaced.
8. Pasted Cloudflare commands are parsed only and never executed. Only a validated Tunnel token is stored in a protected secret file.
9. Installed releases are immutable/versioned and a `current` symlink points at the active release, forming the basis of transactional update/rollback.
10. `rmcp` is the permanent post-install UX for service control, connection information, key rotation, diagnostics, update checking and language selection.

## Layouts

### Standard VPS

```text
/opt/remote-host-mcp/
  current -> releases/<version>-<sha>/
  releases/
/etc/remote-host-mcp/
  rhmcp.env
  install-state.env
  secrets/
/var/lib/remote-host-mcp/
  state/
  runtime/
  backups/
/var/log/remote-host-mcp/
```

### Persistent prefix (DSW/container)

```text
<mnt>/remote-host-mcp/
  current -> releases/<version>-<sha>/
  releases/
  config/
  secrets/
  state/
  runtime/
  logs/
  backups/
```

For ModelScope DSW the recommended root is `/mnt/workspace/remote-host-mcp`.

## Ingress

### Cloudflare Tunnel

`ChatGPT -> Cloudflare -> Tunnel -> cloudflared -> 127.0.0.1:<port> -> Remote Host MCP`

The installer accepts either a raw Tunnel token or the complete Cloudflare `cloudflared service install <token>` / `--token <token>` command, extracts the token and discards the command text.

### Public VPS

`ChatGPT -> HTTPS :443 -> Nginx/Caddy/Apache -> 127.0.0.1:<port> -> Remote Host MCP`

The runtime never binds directly to `0.0.0.0`. A dedicated hostname such as `mcp.example.com` is preferred over a path prefix. One 443 listener can serve many hostnames.

The first 0.2 alpha deliberately uses a conservative reverse-proxy policy: detect and preserve existing proxy ownership and generate isolated templates instead of silently taking over an existing TLS/ACME setup. Automatic clean-host Caddy provisioning and transactional Nginx certificate integration are the next hardening step before a stable installer claim.

## Update model

`rmcp` reads the installed version and can query the repository update channel. The versioned-release/current-symlink layout is created now so update can later install a candidate side by side, health-check it, switch `current`, and rollback without overwriting the previous release.

## Safety invariants

- No broad `pkill`.
- No killing a process just because it owns the preferred port.
- No `eval` of pasted Tunnel commands.
- Secrets remain outside code and are mode 600; secret directories are mode 700.
- No unauthenticated public mode.
- Existing Nginx/Caddy/Apache configuration is not blindly overwritten.
- Public exposure is HTTPS/tunnel/reverse-proxy based; the MCP runtime stays loopback-only.
