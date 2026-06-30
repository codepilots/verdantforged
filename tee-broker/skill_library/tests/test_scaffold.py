def test_workspace_imports():
    import skill_library
    assert skill_library.__file__.endswith("skill_library/__init__.py")


def test_client_fixture_smoke(tmp_db_path, tmp_files_dir, monkeypatch):
    monkeypatch.setenv("SKILL_LIBRARY_DB", str(tmp_db_path))
    monkeypatch.setenv("SKILL_LIBRARY_FILES_DIR", str(tmp_files_dir))
    monkeypatch.setenv("SKILL_LIBRARY_API_KEY", "test-key-123")
    # The client fixture itself can't be invoked yet because create_app doesn't exist.
    # That's fine — Test 3 will land it.
    from skill_library.app import create_app  # noqa: F401
