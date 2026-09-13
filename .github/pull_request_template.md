## What changed

Describe the user/Agent-visible behavior and why it is needed.

## Risk and compatibility

- [ ] No public API/tool/schema change
- [ ] Public behavior changed and documentation was updated
- [ ] High-risk operation changed; rollback/fail-closed behavior is documented
- [ ] Compatibility impact is described

## Validation

- [ ] `pytest -q`
- [ ] `python -m compileall -q src tests`
- [ ] `python scripts/secret_scan.py`
- [ ] Relevant installer/shell checks
- [ ] New or changed behavior has tests

## Operational notes

State any deployment, migration, rollback, or cleanup requirement. Do not include private host data or credentials.
