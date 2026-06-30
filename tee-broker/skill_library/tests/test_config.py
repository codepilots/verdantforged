# tee-broker-deploy/skill_library/tests/test_config.py
import os
from skill_library.config import Config

def test_config_defaults(tmp_db_path, tmp_files_dir, monkeypatch):
    monkeypatch.setenv("SKILL_LIBRARY_DB", str(tmp_db_path))
    monkeypatch.setenv("SKILL_LIBRARY_FILES_DIR", str(tmp_files_dir))
    cfg = Config.from_env()
    assert cfg.db_path == str(tmp_db_path)
    assert cfg.files_dir == str(tmp_files_dir)
    assert cfg.api_key == ""  # default

def test_config_api_key(monkeypatch, tmp_db_path, tmp_files_dir):
    monkeypatch.setenv("SKILL_LIBRARY_DB", str(tmp_db_path))
    monkeypatch.setenv("SKILL_LIBRARY_FILES_DIR", str(tmp_files_dir))
    monkeypatch.setenv("SKILL_LIBRARY_API_KEY", "secret-abc")
    cfg = Config.from_env()
    assert cfg.api_key == "secret-abc"

def test_config_port_default(monkeypatch):
    monkeypatch.delenv("SKILL_LIBRARY_PORT", raising=False)
    cfg = Config.from_env()
    assert cfg.port == 8091

def test_config_port_override(monkeypatch):
    monkeypatch.setenv("SKILL_LIBRARY_PORT", "9099")
    cfg = Config.from_env()
    assert cfg.port == 9099
