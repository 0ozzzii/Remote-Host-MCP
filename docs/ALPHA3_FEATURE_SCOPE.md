# Remote Host MCP 0.2.0-alpha.3 feature scope

Alpha3 adds five canonical tools on top of the alpha2 43-tool surface: `file_artifact`, `ssh_check`, `ssh_exec`, `ssh_upload`, and `ssh_download`.

`file_artifact` returns PNG/JPEG/GIF/WebP files as native MCP `ImageContent` and other small binary files as embedded resources. Large files continue to use `download_info` / `download_chunk`.

Graphical screenshots intentionally remain a two-step workflow: use the existing `exec` tool to invoke a screenshot utility already configured on the host and save the image inside an allowed root, then call `file_artifact` to return that PNG/JPEG directly to the MCP client. Remote Host MCP does not read desktop-session credential files or Xauthority material on the model's behalf.

SSH is deliberately noninteractive and credential-minimizing. The operator pre-provisions OpenSSH config, host keys, agent or IdentityFile settings outside the MCP tool arguments. The MCP tools accept no password, private-key contents, identity-file path, or known-hosts path. They force BatchMode, disable password/keyboard-interactive authentication, and require strict host-key checking. Arbitrary remote command text is sent over SSH stdin rather than placed in the local `ssh` argv.

Production deployment remains deferred until the feature branch passes permanent CI and a separate controlled ModelScope DSW validation.
