# SSH and direct MCP artifacts

## Direct file/image return

`file_artifact(path, max_bytes)` reads one regular file inside `RHMCP_ALLOWED_ROOTS`. PNG, JPEG, GIF and WebP are returned as MCP `ImageContent`; other small binary files are returned as an embedded blob resource. The default inline budget is 4 MiB and the hard ceiling is 8 MiB. Larger objects use the existing resumable/chunked download tools.

A GUI screenshot is intentionally composed from existing primitives rather than introducing desktop credential handling:

1. `exec` invokes a screenshot utility already installed/configured for the active graphical session and writes a PNG inside an allowed root.
2. `file_artifact` returns that PNG as native MCP image content.
3. The temporary PNG can be removed with `remove_path` after the client has received it.

This keeps DISPLAY/Xauthority/session setup an operator/host concern while still allowing a Chat client to receive and render the resulting image.

## Strict OpenSSH client tools

- `ssh_check`: verify a preconfigured target.
- `ssh_exec`: execute one bounded remote shell command.
- `ssh_upload`: freeze an allowed local source, copy to remote staging, publish, then verify SHA-256.
- `ssh_download`: check remote size/SHA-256, copy to private local staging, verify, then publish inside allowed roots.

Security invariants:

- no passwords, private-key contents, identity-file paths or known-hosts paths are accepted as MCP arguments;
- system OpenSSH config / ssh-agent / host-key store are provisioned by the operator;
- `BatchMode=yes`, `PasswordAuthentication=no`, `KbdInteractiveAuthentication=no`, `StrictHostKeyChecking=yes`, `UpdateHostKeys=no`;
- arbitrary remote command text is fed through SSH stdin to `sh -s --`, not exposed in the local ssh argv;
- stdout/stderr remain bounded by the normal Remote Host MCP output budget;
- local file paths remain inside `RHMCP_ALLOWED_ROOTS`; transfer staging lives under the private state directory;
- SSH/SCP timeout or client cancellation terminates the local process group;
- file-transfer success requires SHA-256 agreement.

SSH port forwarding, host-key enrollment, password prompts, interactive key enrollment, and SSH key lifecycle management are intentionally outside this alpha3 tool surface.
