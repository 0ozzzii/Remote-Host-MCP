# Generic Linux deployment profile

Use this profile for a VPS, cloud VM, GPU server or Linux workstation.

## Authority model

Remote Host MCP has the OS authority of the account running it. Running as root gives machine-level control; running as a normal account gives that account's rights. `RHMCP_ALLOWED_ROOTS` constrains the dedicated filesystem/transfer tools only and does not reduce arbitrary shell authority.

For a full-control private host you may explicitly set `RHMCP_ALLOWED_ROOTS=/`. For a narrower deployment, list only the intended persistent/work directories.

## Production layout

Prefer a versioned release directory plus a stable service pointer, for example:

```text
/opt/remote-host-mcp/releases/0.1.0-alpha.1-<sha>/
/var/lib/remote-host-mcp/
/var/log/remote-host-mcp/
/etc/remote-host-mcp/rhmcp.env
```

Create the Python venv in its final release path. Do not move an already-created venv between directories.

## Network

The default listener is loopback. Put a TLS reverse proxy, private tunnel or other controlled ingress in front of it. Do not expose the raw loopback service by changing bind-host to a wildcard; the application intentionally validates loopback-only binding.

## Upgrade pattern

Use a second local port and isolated state for canary verification before switching the production listener. Keep the previous release and service definition until the new version passes health, tools/list, Jobs/Tasks, PTY, filesystem/transfer and restart/no-replay checks.

## Container hosts

If the service itself runs inside a container, the container namespace is the default host boundary. See `../docker/README.md` for namespace/capability considerations.
