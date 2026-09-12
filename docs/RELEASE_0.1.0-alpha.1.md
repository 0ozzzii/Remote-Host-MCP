# Remote Host MCP 0.1.0-alpha.1

Status: **repository migration candidate; real-host validation pending**.

This release establishes the independent Remote Host MCP repository and version line while preserving the proven execution core and DSWD compatibility surface. Repository CI must pass in this repository before it becomes the DSW canary source. Real DSW/VPS/container validation remains a separate gate.

Historical runtime proof stays in `0ozzzii/cf-remote-mcp`; new CI evidence belongs to this repository.

Python distribution and MCP runtime version: `0.1.0a1` (PEP 440 canonical form).
