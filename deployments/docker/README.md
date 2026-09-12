# Docker / container deployment profile

Remote Host MCP can run inside a container. In that case the service controls the namespaces, mounts, devices and capabilities visible to the container's OS identity.

## Default boundary

A normal container deployment controls the container, not the physical host. `process_list`, filesystem paths and service visibility reflect the container namespace. Host-level control requires explicit host namespace/mount/capability exposure and should be treated as equivalent to privileged host administration.

## Security guidance

- Do not use `--privileged` merely to make tests pass.
- Mount only the host paths intentionally managed by RHMCP.
- If `/var/run/docker.sock` is mounted, treat the service as effectively host-root capable.
- Keep the MCP listener private/loopback within the chosen ingress architecture.
- Persist `RHMCP_STATE_DIR` if durable jobs/uploads must survive container recreation.
- PTY sessions do not survive RHMCP process/container restart; durable jobs require the underlying child process namespace to survive as well.

## systemd tools

`service_status/service_action` report unavailable when `systemctl` is absent. That is normal for minimal containers and is not a server failure.

## Host mode

If the intended product is to control the **entire Docker host**, prefer installing RHMCP directly on the host OS instead of constructing a highly privileged container unless isolation/deployment requirements justify the extra complexity.
