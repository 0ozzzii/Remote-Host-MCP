# Security Policy

Remote Host MCP is a high-privilege remote host control plane. Security reports are treated as release-blocking when they affect authentication, authorization boundaries, secret handling, command execution, filesystem containment, process/service control, transfer integrity, SSH behavior, or supply-chain integrity.

## Supported versions

Security fixes are applied to the current development/release line and, when practical, the most recent tagged release. Older alpha or superseded branches are not guaranteed to receive fixes.

| Version | Supported |
|---|---|
| Current `main` / active release candidate | Yes |
| Latest tagged release | Yes, when distinct from `main` |
| Superseded alpha branches | No |

## Reporting a vulnerability

**Do not publish credentials, exploit details, private host information, or working proof-of-concept material in a public issue.**

Preferred reporting path:

1. Use GitHub's private vulnerability reporting / Security Advisory flow for this repository when available.
2. If private reporting is unavailable, open a minimal public issue that contains **no sensitive technical details** and asks the maintainer to establish a private channel.
3. Include affected versions, impact, prerequisites, and a minimal reproduction only after a private channel is established.

Reports involving leaked credentials should identify the credential type and exposure location, but should never repeat the live secret. Rotate/revoke the credential first whenever possible.

## Expected response

The project will acknowledge a credible report, reproduce it where safe, classify affected versions, prepare a fix on a private or restricted path when necessary, and publish remediation guidance after affected credentials or releases are safe to disclose.

## Security design principles

Remote Host MCP follows these baseline rules:

- secrets are configuration/runtime data, not source-code constants;
- public example configuration contains placeholders only;
- filesystem tools are constrained by explicit allowed roots and hardened path handling;
- destructive operations fail closed when identity or state is ambiguous;
- shell execution authority is the OS identity running the service and is never represented as a stronger sandbox than it is;
- capability URLs are credentials and must be handled like passwords;
- OAuth bearer verification does not log token bytes or provider error bodies;
- SSH tools rely on operator-managed OpenSSH trust/identity configuration and do not accept plaintext password/private-key parameters;
- CI performs tests, package validation, current-tree and Git-history secret scanning, and dependency auditing.

## Out of scope

The following are not vulnerabilities by themselves:

- an operator intentionally running the service as `root` and therefore granting root-level shell capabilities;
- an operator setting `RHMCP_ALLOWED_ROOTS=/` for a full-host deployment;
- disclosure of a credential by a user explicitly requesting/displaying it and then publishing their terminal/session output;
- compromise of an external OAuth issuer, reverse proxy, Cloudflare account, SSH agent, or host OS outside Remote Host MCP's control.

Security-impacting behavior caused by Remote Host MCP mishandling those integrations remains in scope.
