from __future__ import annotations

from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "src/remote_host_mcp/filesystem.py"
text = PATH.read_text(encoding="utf-8")


def replace_once(old: str, new: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected exactly one match, got {count}: {old[:80]!r}")
    text = text.replace(old, new, 1)


replace_once(
    '''def _inode_identity(info: os.stat_result) -> tuple[int, int]:
    return (info.st_dev, info.st_ino)
''',
    '''def _inode_identity(info: os.stat_result) -> tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _rollback_exchange_if_ours(
    parent_fd: int,
    temporary_name: str,
    destination_name: str,
    published_inode: tuple[int, int],
) -> bool:
    """Roll back an exchange only while our published inode still owns destination."""
    current = _lstat_at(parent_fd, destination_name)
    temporary = _lstat_at(parent_fd, temporary_name)
    if current is None or temporary is None or _inode_identity(current) != published_inode:
        return False
    rename_exchange(parent_fd, temporary_name, parent_fd, destination_name)
    os.fsync(parent_fd)
    return True
''',
)

old_cas = '''                old_fd = os.open(
                    tmp_name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=parent_fd,
                )
                try:
                    old_info = _fstat_regular(old_fd, message="Destination changed during guarded replacement")
                    old_digest, _ = _hash_fd(old_fd)
                    old_identity = _content_identity(old_info)
                finally:
                    os.close(old_fd)
                if old_digest != expected or old_identity != expected_identity:
                    current_new = _lstat_at(parent_fd, name)
                    if current_new is not None and _inode_identity(current_new) == new_inode:
                        rename_exchange(parent_fd, tmp_name, parent_fd, name)
                        os.fsync(parent_fd)
                    else:
                        # Never destroy an unknown concurrent writer. The old
                        # destination is preserved under the hidden temp name for
                        # operator recovery instead of being unlinked blindly.
                        preserve_tmp = True
                    raise ValueError("Destination changed during expected_sha256 guarded replacement")
                os.unlink(tmp_name, dir_fd=parent_fd)
'''
new_cas = '''                try:
                    old_entry = _lstat_at(parent_fd, tmp_name)
                    if (
                        old_entry is None
                        or not stat.S_ISREG(old_entry.st_mode)
                        or _content_identity(old_entry) != expected_identity
                    ):
                        raise ValueError("Destination changed during expected_sha256 guarded replacement")
                    old_fd = os.open(
                        tmp_name,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=parent_fd,
                    )
                    try:
                        old_info = _fstat_regular(old_fd, message="Destination changed during guarded replacement")
                        old_digest, _ = _hash_fd(old_fd)
                        old_identity = _content_identity(old_info)
                    finally:
                        os.close(old_fd)
                    if old_digest != expected or old_identity != expected_identity:
                        raise ValueError("Destination changed during expected_sha256 guarded replacement")
                except Exception as exc:
                    if not _rollback_exchange_if_ours(parent_fd, tmp_name, name, new_inode):
                        # Never unlink an object belonging to an unknown concurrent
                        # writer. Keep the exchanged entry for operator recovery.
                        preserve_tmp = True
                    if isinstance(exc, ValueError) and str(exc) == "Destination changed during expected_sha256 guarded replacement":
                        raise
                    raise ValueError("Destination changed during expected_sha256 guarded replacement") from exc
                os.unlink(tmp_name, dir_fd=parent_fd)
'''
replace_once(old_cas, new_cas)

old_move = '''            src_inode = _inode_identity(src_stat)
            try:
                rename_exchange(src_fd, src_name, dst_fd, dst_name)
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    raise ValueError("Secure cross-filesystem move is not supported; copy then remove explicitly") from exc
                raise
            try:
                _remove_tree_at(src_fd, src_name)
            except Exception:
                current_dst = _lstat_at(dst_fd, dst_name)
                current_src = _lstat_at(src_fd, src_name)
                if (
                    current_dst is not None
                    and current_src is not None
                    and _inode_identity(current_dst) == src_inode
                ):
                    rename_exchange(src_fd, src_name, dst_fd, dst_name)
                    os.fsync(dst_fd)
                    if src_fd != dst_fd:
                        os.fsync(src_fd)
                raise
'''
new_move = '''            src_inode = _inode_identity(src_stat)
            dst_inode = _inode_identity(dst_stat)
            try:
                rename_exchange(src_fd, src_name, dst_fd, dst_name)
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    raise ValueError("Secure cross-filesystem move is not supported; copy then remove explicitly") from exc
                raise
            exchanged_old = _lstat_at(src_fd, src_name)
            if exchanged_old is None or _inode_identity(exchanged_old) != dst_inode:
                current_dst = _lstat_at(dst_fd, dst_name)
                current_src = _lstat_at(src_fd, src_name)
                if (
                    current_dst is not None
                    and current_src is not None
                    and _inode_identity(current_dst) == src_inode
                ):
                    rename_exchange(src_fd, src_name, dst_fd, dst_name)
                    os.fsync(dst_fd)
                    if src_fd != dst_fd:
                        os.fsync(src_fd)
                raise ValueError("Destination changed during move commit")
            try:
                _remove_tree_at(src_fd, src_name)
            except Exception:
                current_dst = _lstat_at(dst_fd, dst_name)
                current_src = _lstat_at(src_fd, src_name)
                if (
                    current_dst is not None
                    and current_src is not None
                    and _inode_identity(current_dst) == src_inode
                    and _inode_identity(current_src) == dst_inode
                ):
                    rename_exchange(src_fd, src_name, dst_fd, dst_name)
                    os.fsync(dst_fd)
                    if src_fd != dst_fd:
                        os.fsync(src_fd)
                raise
'''
replace_once(old_move, new_move)

old_copy = '''                    rename_exchange(dst_fd, staging_name, dst_fd, dst_name)
                    committed = True
                    try:
                        _remove_tree_at(dst_fd, staging_name)
                    except Exception:
'''
new_copy = '''                    expected_old_inode = _inode_identity(current_dst)
                    rename_exchange(dst_fd, staging_name, dst_fd, dst_name)
                    committed = True
                    exchanged_old = _lstat_at(dst_fd, staging_name)
                    if exchanged_old is None or _inode_identity(exchanged_old) != expected_old_inode:
                        if _rollback_exchange_if_ours(dst_fd, staging_name, dst_name, staged_inode):
                            committed = False
                        else:
                            preserve_staging = True
                        raise ValueError("Destination changed during copy commit")
                    try:
                        _remove_tree_at(dst_fd, staging_name)
                    except Exception:
'''
replace_once(old_copy, new_copy)

PATH.write_text(text, encoding="utf-8")

TEST = ROOT / "tests/test_audit_closure.py"
test = TEST.read_text(encoding="utf-8")
anchor = '''def test_copy_failure_never_destroys_existing_destination(
'''
addition = dedent('''
    def test_expected_sha_symlink_swap_rolls_back_without_touching_victim(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = settings(monkeypatch, tmp_path)
        target = tmp_path / "cas-symlink.txt"
        victim = tmp_path / "victim.txt"
        target.write_text("original", encoding="utf-8")
        victim.write_text("victim", encoding="utf-8")
        expected = hashlib.sha256(b"original").hexdigest()
        real = filesystem.rename_exchange
        injected = False

        def race(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
            nonlocal injected
            if not injected:
                injected = True
                target.unlink()
                target.symlink_to(victim)
            real(src_fd, src, dst_fd, dst)

        monkeypatch.setattr(filesystem, "rename_exchange", race)
        with pytest.raises(ValueError, match="changed during expected_sha256"):
            write_text_file(str(target), "ours", True, expected, 0o600, cfg)
        assert target.is_symlink()
        assert target.resolve() == victim.resolve()
        assert victim.read_text(encoding="utf-8") == "victim"


    def test_copy_overwrite_destination_swap_is_preserved(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = settings(monkeypatch, tmp_path)
        source = tmp_path / "copy-source.txt"
        destination = tmp_path / "copy-destination.txt"
        source.write_text("ours", encoding="utf-8")
        destination.write_text("old", encoding="utf-8")
        real = filesystem.rename_exchange
        injected = False

        def race(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
            nonlocal injected
            if not injected:
                injected = True
                competitor = tmp_path / "copy-competitor.tmp"
                competitor.write_text("competitor", encoding="utf-8")
                os.replace(competitor, destination)
            real(src_fd, src, dst_fd, dst)

        monkeypatch.setattr(filesystem, "rename_exchange", race)
        with pytest.raises(ValueError, match="Destination changed during copy commit"):
            copy_path(str(source), str(destination), False, True, cfg)
        assert destination.read_text(encoding="utf-8") == "competitor"
        assert source.read_text(encoding="utf-8") == "ours"


    def test_move_overwrite_destination_swap_is_preserved(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = settings(monkeypatch, tmp_path)
        source = tmp_path / "move-source.txt"
        destination = tmp_path / "move-destination.txt"
        source.write_text("ours", encoding="utf-8")
        destination.write_text("old", encoding="utf-8")
        real = filesystem.rename_exchange
        injected = False

        def race(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
            nonlocal injected
            if not injected:
                injected = True
                competitor = tmp_path / "move-competitor.tmp"
                competitor.write_text("competitor", encoding="utf-8")
                os.replace(competitor, destination)
            real(src_fd, src, dst_fd, dst)

        monkeypatch.setattr(filesystem, "rename_exchange", race)
        from remote_host_mcp.filesystem import move_path
        with pytest.raises(ValueError, match="Destination changed during move commit"):
            move_path(str(source), str(destination), True, cfg)
        assert source.read_text(encoding="utf-8") == "ours"
        assert destination.read_text(encoding="utf-8") == "competitor"


''')
if test.count(anchor) != 1:
    raise RuntimeError("test insertion anchor mismatch")
test = test.replace(anchor, addition + anchor, 1)
TEST.write_text(test, encoding="utf-8")

DOC = ROOT / "docs/AUDIT_CLOSURE_20260913.md"
doc = DOC.read_text(encoding="utf-8")
needle = "- Race-free `overwrite=false` publication uses Linux `renameat2(RENAME_NOREPLACE)` for text writes, copies, moves and upload finalization.\n"
replacement = needle + "- Exchange-based overwrite/CAS commits verify the exact displaced inode and roll back if a concurrent destination swap wins the pre-commit race, including symlink/type swaps.\n"
if doc.count(needle) != 1:
    raise RuntimeError("doc insertion anchor mismatch")
DOC.write_text(doc.replace(needle, replacement, 1), encoding="utf-8")

Path(__file__).unlink(missing_ok=True)
(ROOT / ".github/workflows/audit-toctou-followup.yml").unlink(missing_ok=True)
