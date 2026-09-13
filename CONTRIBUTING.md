# Contributing to Remote Host MCP

Thanks for contributing. Remote Host MCP is a high-privilege host-control project, so changes are reviewed for correctness, rollback behavior, compatibility, and operational safety as well as feature behavior.

## Development setup

Requirements: Python 3.10+, Git, Bash, and a Linux development environment.

```bash
git clone https://github.com/0ozzzii/Remote-Host-MCP.git
cd Remote-Host-MCP
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -e '.[dev]'
```

## Before changing code

1. Read `README.md` and the relevant design document under `docs/`.
2. Inspect existing tests and tool schemas before changing behavior.
3. Keep machine-specific configuration and private operational data outside the repository.
4. Prefer bounded, explicit operations over unbounded host scans or implicit side effects.
5. Preserve rollback and fail-closed behavior for high-risk changes.

## Local validation

```bash
pytest -q
python -m compileall -q src tests
python scripts/secret_scan.py
bash -n install.sh
bash -n scripts/rmcp.sh
find installer -type f -name '*.sh' -print0 | xargs -0 -n1 bash -n
```

Installer changes should also run:

```bash
pytest -q tests/test_installer_contract.py
```

Changes that affect authentication, filesystem mutation, process/service control, SSH, release plumbing, or sensitive-data handling should also run:

```bash
python scripts/secret_scan.py --git-history
```

CI additionally validates supported Python versions, dependency health, package build/install integrity, the exact MCP tool surface, live MCP wire behavior, installer contracts, and targeted security regressions.

## Pull requests

A pull request should explain the Agent/user-visible change, identify compatibility impact, include tests, avoid unrelated refactors, state rollback behavior for high-risk changes, and update public documentation when behavior changes.

Keep the canonical tool manifest and MCP schemas synchronized whenever the tool surface changes.

## Compatibility

The canonical product namespace is `RHMCP_*` / `remote_host_mcp`. Legacy `DSW_MCP_*` and `dsw_direct_mcp` compatibility exists for migration and should not be expanded without a specific compatibility requirement.

## Security reports

Follow `SECURITY.md` rather than publishing vulnerability details in a normal issue.

## License

Contributions are made under the Apache License 2.0, consistent with the repository `LICENSE`.
