# Remote Host MCP 0.1.0a1 — DSW Canary / Cutover Runbook

Status: **PREPARED — DO NOT EXECUTE UNTIL DSWC IS AVAILABLE AND PHASE 0 INVENTORY IS COMPLETE**

This is the execution checklist for installing Remote Host MCP 0.1.0a1 on the existing ModelScope DSW while preserving the current DSWD 2.x service as a rollback source. DSWC is the control/maintenance channel for this migration.

## Gate A — evidence required before touching DSW

The GitHub candidate must have all of the following:

- package/version `remote-host-mcp 0.1.0a1`;
- canonical namespace `remote_host_mcp`;
- canonical CLI `remote-host-mcp`;
- 43 tools;
- MCP 2026-07-28 modern wire PASS;
- generic serverInfo/health/tool copy PASS;
- real MCP Tasks wire PASS;
- DSWD compatibility namespace/CLI PASS;
- full pytest + compileall PASS;
- static destructive/secret checks PASS;
- recorded validated Git SHA and CI run.

If any item is missing, do not deploy.

## Phase 0 — read-only real-machine inventory via DSWC

Do not modify anything in this phase.

Record:

1. `/mnt/workspace` is mounted/writable and available disk space.
2. Existing DSWD PID, `/proc/<pid>/stat` start_ticks, cmdline and 8765 listener.
3. Existing DSWD source/runtime paths and version/SHA if available.
4. `.env` path, permissions, size, mtime and cryptographic hash. Never print the key/token values.
5. Existing state/log directories and non-terminal Jobs/PTYs if observable.
6. Exact contents of `/mnt/workspace/scripts/restore_dsw_direct.sh` and the relevant DSWD section of `restore_connectivity.sh`.
7. cloudflared PID identity, binary, token-file path metadata and configured origin. Do not print the token.
8. DSWC status/fallback health.
9. Python version/kernel/seccomp-relevant facts needed for openat2/pidfd verification.
10. Any discrepancy between Drive/Git documentation and the real host.

If the old restore script still starts `python -m dsw_direct_mcp.server`, record it as the current rollback behavior; do not edit it yet.

## Phase 1 — rollback point

Create a timestamped persistent rollback directory under `/mnt/workspace` or the existing backup convention. Capture only what is necessary:

- pre-change restore script;
- current code/release reference;
- `.env` backup with mode 600;
- state manifest/snapshot suitable for currently active jobs;
- old localhost health/tool evidence;
- old PID/start_ticks/listener evidence.

Keep DSWC and cloudflared untouched.

## Phase 2 — install isolated 3.0 release

Recommended target:

```text
/mnt/workspace/rhmcp-releases/0.1.0a1-<VALIDATED_SHA>/
```

Create the Python runtime in its final path, e.g.:

```text
/mnt/workspace/rhmcp-releases/0.1.0a1-<SHA>/runtime/
```

Do not transplant a venv from `/root`, `/tmp` or another release path.

Preserve the existing capability key and public hostname. The first real DSW deployment keeps OAuth OFF.

## Phase 3 — 8766 canary

Use:

```text
RHMCP_BIND_HOST=127.0.0.1
RHMCP_PORT=8766
RHMCP_STATE_DIR=/mnt/workspace/rhmcp-canary-state
```

The canary must not share the production state directory and must not start another cloudflared process.

### Canary acceptance

Read-only first:

- `GET /health` => service `Remote Host MCP`, version `0.1.0a1`;
- MCP serverInfo name/title `Remote Host MCP`;
- tools/list = exactly 43;
- `status`;
- `system_info`;
- `list_directory`;
- `process_list`.

Dedicated temporary write area under `/mnt/workspace`:

- create/write/read/hash/remove a test file;
- upload_begin/chunk/status/finish + download_info/chunk + SHA-256 equality;
- root-internal symlink read succeeds;
- deliberately outward symlink is rejected;
- transfer cannot commit through an escape parent.

Durable job:

- `job_run` a unique harmless command;
- poll status/read with cursors;
- verify exact output/exit status;
- repeat same idempotency key/request and confirm no duplicate execution;
- Tasks-aware request returns taskId and `tasks/get` reaches completed.

PTY:

- open;
- `pwd`, `cd`, env continuity;
- normal command completes via `osc133` with exact exit code;
- output contains no OSC marker;
- start `sleep`, send INT, verify completion and shell remains usable;
- close and verify no leftover test shell.

Process:

- create a dedicated `sleep` test process;
- `process_info` captures PID/start_ticks;
- signal only that exact identity;
- verify no unrelated process touched.

Kernel hardening:

- record whether openat2 native path or fallback is active;
- record whether pidfd native path or fallback is active;
- either is acceptable if the validated safety fallback is observed.

## Phase 4 — production cutover to 8765

Only after every canary item passes:

1. Stop the canary exactly; verify 8766 is free and no test processes remain.
2. Record the old 8765 DSWD PID identity again immediately before cutover.
3. Stop only that exact old process.
4. Verify 8765 is free.
5. Start validated RHMCP 0.1.0a1 on 8765.
6. Verify health **and** 43-tool Mature/full surface. Version alone is not enough.
7. Verify cloudflared PID is unchanged and still targets 8765.
8. Verify public `/health` and public MCP tools/list.
9. Refresh/reconnect ChatGPT; read-only call first, then limited write smoke.

## Phase 5 — restart/no-replay acceptance

Start a durable job that:

- writes a unique marker once;
- runs long enough to restart RHMCP;
- has recorded job_id, worker/child PID+start_ticks and log cursor.

Restart only RHMCP. Expected:

- same child process remains if appropriate;
- new server reconciles/observes the old job;
- command is never replayed;
- unique marker appears exactly once;
- job can still be read/cancelled to terminal state.

PTYs are allowed to end on RHMCP restart; that is documented behavior.

## Phase 6 — persistent recovery switch

Only after Phase 5 passes:

1. backup `restore_dsw_direct.sh` again;
2. change only its exact DSWD startup target to the validated 3.0 canonical release entrypoint;
3. do not rewrite unrelated connectivity recovery;
4. syntax-check;
5. read back the changed file;
6. invoke the recovery path once through DSWC;
7. verify local/public MCP and exact process identity.

## Phase 7 — full DSW restart

The final production gate is one ModelScope DSW stop/start or restart cycle:

- `/mnt/workspace` persists;
- global recovery executes;
- RHMCP 0.1.0a1 recovers on 8765;
- cloudflared recovers exactly once;
- ChatGPT reconnects;
- no stray canary/server/tunnel process remains.

Only after this phase can the DSW deployment be labeled `DSW VERIFIED / PRODUCTION-READY`.

## Immediate rollback procedure

At any failure after production cutover:

1. freeze further writes;
2. capture 3.0 logs/state/PID evidence;
3. stop exact 3.0 PID;
4. verify 8765 is free;
5. restore old `restore_dsw_direct.sh` or old startup pointer;
6. start preserved DSWD 2.x runtime;
7. verify old local surface/public MCP;
8. keep 3.0 evidence for diagnosis.

Do not roll back Cloudflare, DNS, SSH, DSWC, COMSOL/Abaqus or other fault domains unless independent evidence shows they are involved.
