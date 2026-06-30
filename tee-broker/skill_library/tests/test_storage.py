import os
from skill_library.storage import (
    path_for_blob,
    write_blob,
    read_blob,
    delete_blob,
)

def test_path_for_blob_stable(tmp_files_dir):
    p = path_for_blob(tmp_files_dir, "summarize", "1.0.0", "SKILL.md")
    assert str(p).endswith("summarize/1.0.0/SKILL.md")

def test_write_read_roundtrip(tmp_files_dir):
    p = path_for_blob(tmp_files_dir, "summarize", "1.0.0", "SKILL.md")
    write_blob(p, b"hello world")
    assert read_blob(p) == b"hello world"

def test_write_creates_parent_dirs(tmp_files_dir):
    p = path_for_blob(tmp_files_dir, "a", "0.1.0", "deep/nested/file.txt")
    write_blob(p, b"nested")
    assert read_blob(p) == b"nested"
    assert p.exists()

def test_delete_missing_is_noop(tmp_files_dir):
    p = path_for_blob(tmp_files_dir, "ghost", "0.0.0", "missing.bin")
    delete_blob(p)
    assert not p.exists()
