import pytest


@pytest.fixture
def tmp_db_path(tmp_path):
    return tmp_path / "skill_library.db"


@pytest.fixture
def tmp_files_dir(tmp_path):
    d = tmp_path / "files"
    d.mkdir()
    return d


@pytest.fixture
def tmp_efs(tmp_path):
    return tmp_path


@pytest.fixture
def client(tmp_db_path, tmp_files_dir, monkeypatch):
    monkeypatch.setenv("SKILL_LIBRARY_DB", str(tmp_db_path))
    monkeypatch.setenv("SKILL_LIBRARY_FILES_DIR", str(tmp_files_dir))
    monkeypatch.setenv("SKILL_LIBRARY_API_KEY", "test-key-123")
    from skill_library.app import create_app
    app = create_app()
    from fastapi.testclient import TestClient
    return TestClient(app)
