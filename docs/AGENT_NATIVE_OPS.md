# Agent-native operations (0.2.0-alpha.4)

Remote Host MCP 0.2.0-alpha.4 adds a bounded Agent-oriented layer on top of the existing shell, filesystem, durable jobs, PTY, process/service, artifact, and SSH surfaces. The goal is fewer speculative calls, safer mutations, and better result handoff rather than SSH feature parity.

## New tools

- `host_capabilities`: secret-free capability matrix for planning.
- `wait_condition`: bounded waits for file/job/process/loopback-port/log conditions.
- `artifact_info`, `artifact_preview`, `artifact_bundle`: metadata, bounded text preview, and no-clobber ZIP bundling.
- `exec_argv`: direct argv execution without shell parsing, using a minimal child environment plus an explicit non-secret allowlist.
- `snapshot_create`, `snapshot_list`, `snapshot_restore`, `snapshot_delete`: persistent scoped file rollback points.
- `lease_acquire`, `lease_status`, `lease_list`, `lease_release`: TTL coordination leases for multi-Agent work.
- `inspect_paths`: at most 20 explicit paths with bounded metadata/hash/text preview; no host-wide recursion.
- `file_diff`, `apply_patch`: bounded unified diff and SHA-guarded line edits with atomic final publication.

## Safety boundaries

- All path-bearing tools remain constrained by `RHMCP_ALLOWED_ROOTS`; snapshots are internal to `RHMCP_STATE_DIR`.
- Snapshot creation rejects symlinks and special files and is bounded by path count, node count, and total bytes. Restore is atomic per captured top-level path; it is not a single cross-path transaction.
- `wait_condition(port_open)` is loopback-only and therefore is not a remote port scanner. Log matching is literal and bounded.
- `exec_argv` never invokes a shell. It caps argv/stdin/output/time and rejects secret-like environment names from explicit inheritance.
- Leases automatically expire and are coordination primitives, not authorization controls.
- Batch inspection never recursively walks directories. Hashing and previews have explicit per-file budgets.
- `apply_patch` requires the previously observed SHA-256; a concurrent edit causes a fail-closed mismatch.
- Artifact bundles reject symlinks/special files and never overwrite an existing destination.

These tools supplement rather than replace the lower-level primitives. Agents should prefer `exec_argv` over shell `exec` when shell syntax is unnecessary, use snapshots before multi-file risky edits, and use durable jobs for work that may exceed the synchronous request deadline.
