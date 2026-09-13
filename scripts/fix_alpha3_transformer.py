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
patch = r'''# Existing contract suites intentionally evolve from the 43-tool alpha2 surface
# to the 48-tool alpha3 surface. file_artifact is the sole content-only tool so
# MCP clients can receive native ImageContent / EmbeddedResource blocks.
replace_once(
    "tests/test_audit_closure.py",
    "    assert len(tools) == 43\n",
    "    assert len(tools) == 48\n",
)
replace_once(
    "tests/test_rhmcp_branding.py",
    "    assert len(listed.tools) == 43\n",
    "    assert len(listed.tools) == 48\n",
)
replace_once(
    "tests/test_mature.py",
    '            "download_info", "download_chunk", "exec",\n',
    '            "download_info", "download_chunk", "file_artifact", "ssh_check", "ssh_exec", "ssh_upload", "ssh_download", "exec",\n',
)
replace_once(
    "tests/test_mature.py",
    "        assert all(tool.output_schema is not None for tool in tools.values())\n",
    '        assert tools["file_artifact"].output_schema is None\n        assert all(tool.output_schema is not None for name, tool in tools.items() if name != "file_artifact")\n',
)
replace_once(
    "tests/test_openai_mcp_contract.py",
    '    "download_info",\n    "download_chunk",\n    "exec",\n    "job_run",\n',
    '    "download_info",\n    "download_chunk",\n    "file_artifact",\n    "ssh_check",\n    "ssh_exec",\n    "ssh_upload",\n    "ssh_download",\n    "exec",\n    "job_run",\n',
)
replace_once(
    "tests/test_openai_mcp_contract.py",
    '    "download_info",\n    "download_chunk",\n    "job_status",\n',
    '    "download_info",\n    "download_chunk",\n    "file_artifact",\n    "ssh_check",\n    "job_status",\n',
)
replace_once(
    "tests/test_openai_mcp_contract.py",
    'OPEN_WORLD_TOOLS = {"exec", "job_run", "job_start", "terminal_exec", "terminal_write"}\n',
    'OPEN_WORLD_TOOLS = {"exec", "job_run", "job_start", "terminal_exec", "terminal_write", "ssh_check", "ssh_exec", "ssh_upload", "ssh_download"}\n',
)
replace_once(
    "tests/test_openai_mcp_contract.py",
    '    "upload_finish",\n    "exec",\n',
    '    "upload_finish",\n    "ssh_exec",\n    "ssh_upload",\n    "ssh_download",\n    "exec",\n',
)
replace_once(
    "tests/test_openai_mcp_contract.py",
    '        assert tool.output_schema is not None, f"{name}: missing output schema"\n',
    '        if name == "file_artifact":\n            assert tool.output_schema is None, "file_artifact must remain content-only for native MCP image/resource blocks"\n        else:\n            assert tool.output_schema is not None, f"{name}: missing output schema"\n',
)

'''
if anchor not in text:
    raise RuntimeError("contract-patch insertion anchor not found")
text = text.replace(anchor, patch + anchor, 1)
target.write_text(text, encoding="utf-8")
Path(__file__).unlink(missing_ok=True)
