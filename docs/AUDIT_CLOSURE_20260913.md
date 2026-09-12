# Remote Host MCP audit closure — 2026-09-13

This change set closes the remaining findings consolidated from the functional smoke test and the two code/security reviews. It is a code/CI candidate only; production DSW deployment is intentionally deferred.

## Code changes

- Race-free `overwrite=false` publication uses Linux `renameat2(RENAME_NOREPLACE)` for text writes, copies, moves and upload finalization.
- Guarded text replacement uses `RENAME_EXCHANGE` plus verification/rollback so a concurrent destination replacement is not destroyed.
- `copy_path(overwrite=true)` stages the entire copy before atomic publication; failed copies leave the previous destination intact.
- `make_directory(exist_ok=true)` no longer chmods an already-existing directory.
- `read_text_file` scans with fixed-size `os.read` chunks so a single unbroken line cannot bypass the memory budget.
- Generic/legacy configuration namespaces fail closed when both are present with different values; error text contains variable names, never values.
- OSC 133 partial-marker state has a strict 256-byte ceiling.
- Durable job cleanup leaves an idempotency tombstone; an already-consumed key cannot replay arbitrary shell after cleanup.
- Durable job cancellation validates both PID start ticks and process-group identity before group signalling.
- High-risk signal/action schemas expose explicit enums; canonical tool schemas reject unknown top-level arguments.
- Status/health expose optional build commit/ref provenance. `remote-host-mcp-doctor` emits read-only, secret-free machine diagnostics.
- Exact 43-tool manifest is pinned in `tests/tool_manifest.json`.

## Findings closed by verification rather than rewrite

- `upload_abort` already refuses committed uploads and only removes staging data; regression coverage now protects that invariant.
- Filesystem traversal already uses `openat2(RESOLVE_BENEATH|RESOLVE_NO_MAGICLINKS)` with stable dirfds and a conservative fallback; the new atomic-publish primitives close the remaining destination-entry race.
- OAuth mode is a real external OAuth/OIDC resource-server integration using MCP SDK `AuthSettings` plus JWT issuer/audience/signature/expiry checks; capability mode does not pretend to be OAuth.
- Short exec cancellation already terminates/reaps its process group; terminal close already terminates foreground work and joins the reader thread.
- Durable recovery already observes persisted PID+start-ticks state and never replays `command.bin`; the new tombstone closes replay after cleanup.

## Release identity

Candidate version: `0.2.0-alpha.2` / Python `0.2.0a2`.
Production DSW remains unchanged until a separate controlled deployment and targeted post-deploy validation.
