# Changelog

All notable user-visible changes to Remote Host MCP are tracked here.

The project follows semantic versioning where practical. Pre-release identifiers (`alpha`, `beta`, `rc`) indicate that compatibility or deployment behavior may still change before a stable release.

## [Unreleased]

### Security and release hardening
- Added Apache-2.0 licensing and public security/contribution/support policies.
- Added release-oriented secret scanning, dependency auditing, Python compatibility validation, and package validation.
- Added verified Cloudflare binary download support with a pinned upstream version and SHA-256 checks.
- Added explicit non-interactive connection display behavior for safer scripting while preserving an intentional way to reveal the full capability URL.
- Removed stale hard-coded product-version banners from runtime management scripts.

## [0.2.0-alpha.4] - 2026-09-13

### Added
- Expanded the canonical MCP surface to 65 tools.
- Added Agent-native capability discovery, bounded waiting, artifacts, argv execution, scoped snapshots, TTL leases, bounded multi-path inspection, diff, and SHA-guarded patching.
- Added direct MCP image/binary artifact return.
- Added strict OpenSSH-based cross-host check/exec/upload/download tools.

### Security
- Retained process argument redaction, hardened path handling, atomic publication, service-manager fail-closed behavior, durable-job identity controls, schema validation, and build provenance checks.

### Validation
- 83-test permanent CI acceptance, live MCP inspector verification, exact 65-tool contract validation, installer validation, and targeted repair/security validation passed on the accepted alpha4 candidate.

## [0.2.0-alpha.1] - 2026-09-12

- Added the bilingual installer, standard and persistent layouts, ingress selection, capability/OAuth configuration, `rmcp` management UX, update checking, and installer CI.

## [0.1.0-alpha.1]

- Initial independently packaged Remote Host MCP release line.
