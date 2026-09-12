# Remote Host MCP 0.1.0-alpha.1 — Rollback and Compatibility

## Git-level rollback

The generic 3.0 work is isolated on branch `remote-host-mcp-v3`.

Known pre-generalization baseline:

- DSWD 2.1 branch: `direct-dsw-v1`
- pre-RHMCP rollback branch/tag-equivalent created during refactor: `archive/dswd-v2.1-pre-rhmcp-20260912`
- the generic branch was created from the intact DSWD 2.1 baseline rather than rewriting it.

Do not force-update or delete the 2.1 baseline during the first DSW deployment.

## Runtime compatibility

Remote Host MCP 0.1.0-alpha.1 retains:

- `dsw-direct-mcp` console alias;
- `dsw_direct_mcp.*` import namespace through package-path compatibility;
- `DSW_MCP_*` environment compatibility;
- the same durable-job state semantics and tool names.

This compatibility exists to make controlled migration possible. New deployments should use the canonical 3.0 names.

## Configuration precedence

During migration, an existing `DSW_MCP_*` variable wins if both old and new names are present. This prevents adding a generic `RHMCP_*` value from silently changing a live DSWD installation. A clean 3.0 installation should use only `RHMCP_*`.

## DSW application rollback

The real DSW rollout is intentionally non-destructive:

- old `/mnt/workspace/dsw-direct-mcp` remains intact;
- new 3.0 is installed under a versioned release path;
- 8766 canary uses isolated state;
- production Tunnel remains pointed at 8765;
- restore script is changed last.

If 3.0 fails after cutover, stop its exact PID, free 8765, restore the old startup target and start the preserved DSWD 2.x runtime. Do not delete the failed release/state/logs before diagnosis.

## OAuth rollback

The first DSW deployment does not enable OAuth. In future deployments, if OAuth integration fails, switch only the auth layer back to capability mode and restore the previous capability endpoint. Do not roll back Jobs, state or the network tunnel solely because an external IdP failed.

## Kernel-feature fallback

openat2 and pidfd are hardening accelerators with validated fallbacks. An older kernel or restrictive seccomp profile is not itself a reason to roll back if:

- filesystem operations still enforce canonical allowed-root + fd-target checks; and
- signals still require exact PID+start_ticks identity.

Never solve compatibility by disabling path confinement or identity checks.

## Evidence before rollback

Before reverting a failed deployment, capture without secrets:

- exact 3.0 SHA/version;
- PID/start_ticks/cmdline/listener;
- health and tools/list result;
- relevant logs;
- job/PTY state;
- whether native openat2/pidfd or fallback was active;
- public/local connectivity split.

Rollback is for service restoration; preserving evidence is necessary for root-cause analysis.
