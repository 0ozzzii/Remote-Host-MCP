# Remote Host MCP 0.2.0-alpha.1

Development prerelease for the bilingual production installer and persistent `rmcp` management UX.

Main additions:

- first-screen Chinese/English language selection;
- standard VPS vs persistent-prefix/DSW layouts;
- preferred-port 8765 detection with safe next-free/custom fallback;
- explicit root/full-host vs current-user authority profiles;
- capability URL default, OAuth advanced, no unauthenticated public mode;
- public-domain vs Cloudflare Tunnel ingress selection;
- safe parsing of pasted Cloudflare install commands without shell evaluation;
- versioned release directories plus `current` symlink;
- persistent install-state metadata and secret separation;
- `rmcp` menu for service control, connection details, key rotation, diagnostics, update checking and language selection;
- conservative reverse-proxy detection for Nginx/Caddy/Apache without blind takeover of existing 443/TLS configuration.

The 43-tool MCP runtime remains the validated baseline; this release line primarily productizes deployment and lifecycle management.
