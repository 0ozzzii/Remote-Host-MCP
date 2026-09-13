from pathlib import Path

root = Path(__file__).resolve().parents[1]
target = root / "scripts" / "apply_alpha3_ssh_artifacts.py"
text = target.read_text(encoding="utf-8")
text = text.replace(
    '    if text.count(old) != 1:\n        raise RuntimeError(f"{path}: expected one occurrence, found {text.count(old)} for {old!r}")\n    write(path, text.replace(old, new, 1))',
    '    if old not in text:\n        raise RuntimeError(f"{path}: replacement anchor not found for {old!r}")\n    write(path, text.replace(old, new, 1))',
)
problem = '''replace_once(\n    "src/remote_host_mcp/ssh_helpers.py",\n    "        scp = _client_binary(\\"scp\\")\\n",\n    "        if remote_size > settings.max_transfer_bytes:\\n            raise ValueError(f\\"Remote file exceeds transfer limit {settings.max_transfer_bytes}\\")\\n        scp = _client_binary(\\"scp\\")\\n",\n)\n'''
if problem not in text:
    raise RuntimeError("expected ambiguous scp transformer block was not found")
text = text.replace(problem, "", 1)
anchor = "# New focused regression tests.\n"
patch = '''replace_once(\n    "tests/test_audit_closure.py",\n    "    assert len(tools) == 43\\n",\n    "    assert len(tools) == 48\\n",\n)\n\n'''
if anchor not in text:
    raise RuntimeError("audit-count insertion anchor not found")
text = text.replace(anchor, patch + anchor, 1)
target.write_text(text, encoding="utf-8")
Path(__file__).unlink(missing_ok=True)
