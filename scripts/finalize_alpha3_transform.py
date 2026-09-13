from pathlib import Path

root = Path(__file__).resolve().parents[1]
path = root / "src" / "remote_host_mcp" / "ssh_helpers.py"
text = path.read_text(encoding="utf-8")
start = text.index("async def ssh_download(")
needle = '        scp = _client_binary("scp")\n'
pos = text.index(needle, start)
insert = (
    '        if remote_size > settings.max_transfer_bytes:\n'
    '            raise ValueError(f"Remote file exceeds transfer limit {settings.max_transfer_bytes}")\n'
)
text = text[:pos] + insert + text[pos:]
path.write_text(text, encoding="utf-8")
Path(__file__).unlink(missing_ok=True)
