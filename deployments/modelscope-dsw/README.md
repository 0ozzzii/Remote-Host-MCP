# ModelScope DSW deployment profile

This profile deploys **Remote Host MCP 0.1.0-alpha.1** onto a ModelScope DSW instance while preserving the existing DSWD 2.x installation as an immediate rollback source.

## Naming

- Product: Remote Host MCP 0.1.0-alpha.1
- Deployment/profile name: DSWD
- Existing live service: legacy DSWD 2.x on `127.0.0.1:8765`
- New canary: RHMCP 0.1.0-alpha.1 on `127.0.0.1:8766`
- Persistent root: `/mnt/workspace`

DSWD is now a deployment profile, not the generic product name.

## Non-negotiable cutover rules

1. Use **DSWC** to operate/maintain the DSWD/RHMCP service during the migration. Do not use the service being replaced to replace itself.
2. Do not overwrite `/mnt/workspace/dsw-direct-mcp` in place.
3. Keep the old release, old runtime, `.env`, state, logs and restore scripts until the entire acceptance test is complete.
4. Install 3.0 into a versioned release directory under `/mnt/workspace`.
5. Canary on port 8766 with a separate state directory. Never let two server processes manage the same durable state simultaneously.
6. Stop/start processes by exact PID identity. Never use broad `pkill -f`.
7. Do not restart or replace the existing Cloudflare Tunnel during the application cutover. The production tunnel continues to target `127.0.0.1:8765`.
8. Update the persistent restore script only after 8765 production validation and restart/no-replay testing pass.
9. OAuth remains OFF during the first real DSW deployment. Use the existing capability URL to reduce migration variables.
10. Do not touch COMSOL, Abaqus, RustDesk, SSH, DSWC or unrelated recovery domains during RHMCP smoke tests.

## Recommended persistent layout

```text
/mnt/workspace/
├── dsw-direct-mcp/                         # existing DSWD 2.x, preserved
├── rhmcp-releases/
│   └── 0.1.0-alpha.1-<validated-sha>/
│       ├── source/
│       └── runtime/
├── rhmcp-state/                            # production 3.0 state after cutover
├── rhmcp-canary-state/                     # disposable 8766 state
└── scripts/
    ├── restore_dsw_direct.sh               # production pointer, changed last
    └── restore_dsw_direct.sh.bak_<stamp>   # rollback copy
```

The exact existing directories must be discovered on the real DSW before any write; do not create duplicates based only on this example.

## Phase 0 — DSWC read-only inventory

Before installing anything, record without printing secrets:

- `/mnt/workspace` mount and writability;
- exact old DSWD PID, start_ticks, cmdline and 8765 listener;
- old package/version/Git SHA if available;
- `.env` path, mode, size, mtime and hash only;
- state/log/runtime locations;
- `restore_dsw_direct.sh` and `restore_connectivity.sh` contents;
- cloudflared PID identity and origin target;
- DSWC health/fallback;
- active/non-terminal jobs and PTYs if observable;
- free disk and Python version.

If documentation and the real machine disagree, the real machine wins and the migration pauses until the difference is understood.

## Phase 1 — rollback point

Create timestamped backups of exactly the assets that will be changed:

- current startup/recovery script;
- current code tree or an immutable reference to its SHA;
- `.env` backup with mode 600;
- state snapshot/manifest appropriate to current active jobs;
- current localhost/public health evidence.

Do not put secret values in Drive, Git or normal logs.

## Phase 2 — install isolated 3.0 release

Use the validated `remote-host-mcp-v3` SHA. Put source and the Python runtime directly in the final versioned `/mnt/workspace/rhmcp-releases/...` path; do not create a venv elsewhere and move it afterward.

Use `deployments/modelscope-dsw/rhmcp.env.example` as the mapping reference, but preserve the current capability key/public hostname rather than generating a new credential during migration.

## Phase 3 — 8766 canary

Start exactly one RHMCP 0.1.0-alpha.1 canary:

- bind `127.0.0.1`;
- port `8766`;
- isolated `RHMCP_STATE_DIR`;
- same allowed roots intended for production;
- capability auth;
- no second cloudflared process.

Acceptance:

- `/health` reports `Remote Host MCP`, version 0.1.0-alpha.1;
- MCP serverInfo name/title = `Remote Host MCP`;
- tools/list exactly 43;
- `status`, `system_info`, `list_directory`, `process_list` read-only smoke;
- dedicated temporary filesystem write/read/hash/remove;
- resumable upload/download in the same temporary area;
- `job_run` normal completion and cursor read;
- MCP Tasks-aware `job_run → taskId → tasks/get → completed`;
- PTY open → cwd continuity → normal OSC 133 completion → Ctrl+C → close;
- process_info + signal against a deliberately created test process only;
- openat2 in-root symlink works and escape symlink is refused;
- pidfd path is used if kernel/seccomp permits, otherwise the validated PID+start_ticks fallback is observed.

## Phase 4 — production port cutover

Only after canary acceptance:

1. stop the 8766 canary;
2. verify canary state is not the production state;
3. precisely stop the old 8765 DSWD by recorded PID identity;
4. verify 8765 is free;
5. start RHMCP 0.1.0-alpha.1 on 8765 with the production configuration/state choice;
6. verify health **and** 43 tools — version alone is insufficient;
7. verify existing cloudflared remains the same process and public health succeeds;
8. refresh the MCP client and perform read-only calls before any write smoke.

## Phase 5 — restart/no-replay

Create a unique long-running durable job, record `job_id`, PID/start_ticks and output cursor, then restart **only RHMCP**. The job must be observed/reconciled without re-running its command. A unique marker must appear once, never twice.

PTYs are expected to end when the server process restarts; durable jobs are the restart-surviving execution model.

## Phase 6 — persistent recovery pointer

Only after all preceding checks pass:

- back up the current `restore_dsw_direct.sh`;
- change only the exact startup target to the validated 3.0 release/canonical entrypoint;
- syntax-check and read back the file;
- invoke it once through DSWC and verify localhost/public MCP again.

## Rollback

If any production check fails:

1. precisely stop RHMCP 0.1.0-alpha.1;
2. verify 8765 is free;
3. restore the old recovery script/startup target;
4. start the preserved old DSWD 2.x runtime;
5. verify old localhost health/tool surface and public connectivity;
6. keep the failed 3.0 release/state/logs for diagnosis; do not delete evidence.

Cloudflare, DNS, DSWC and unrelated machine services are not part of the application rollback.

## Production-ready definition

Do not mark the DSW deployment production-ready until localhost, 43-tool surface, filesystem/transfer, Jobs/Tasks, PTY, process signaling, restart/no-replay, Cloudflare public MCP, ChatGPT calls and one full ModelScope DSW restart/recovery test have all passed.
