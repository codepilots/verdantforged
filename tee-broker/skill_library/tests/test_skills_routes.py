"""Tests for skill_library.routes (healthz + skills).

These tests build their own FastAPI app inline and mount the routers,
because `skill_library.app.create_app` is added in T7. The conftest.py
`client` fixture will start working once T7 lands and the routers are
wired into the real app factory.
"""
import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

AUTH = {"Authorization": "Bearer test-key-123"}


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture
def app(tmp_db_path, tmp_files_dir, monkeypatch):
    monkeypatch.setenv("SKILL_LIBRARY_DB", str(tmp_db_path))
    monkeypatch.setenv("SKILL_LIBRARY_FILES_DIR", str(tmp_files_dir))
    monkeypatch.setenv("SKILL_LIBRARY_API_KEY", "test-key-123")
    monkeypatch.delenv("BROKER_BASE_URL", raising=False)
    monkeypatch.delenv("BROKER_SKILLS_API_KEY", raising=False)
    from skill_library.config import Config
    from skill_library.routes.healthz import router as healthz_router
    from skill_library.routes.skills import router as skills_router

    test_app = FastAPI()
    test_app.state.config = Config.from_env()
    test_app.include_router(healthz_router)
    test_app.include_router(skills_router)
    return TestClient(test_app)


def test_healthz(app):
    r = app.get("/healthz")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["db"] == "ok"
    assert body["efs"] == "ok"


def test_register_skill_requires_auth(app):
    r = app.post(
        "/v1/library/skills",
        json={
            "name": "summarize",
            "version": "1.0.0",
            "description": "Summarize text",
        },
    )
    assert r.status_code == 401, r.text


def test_register_skill_with_bad_version_is_400(app):
    r = app.post(
        "/v1/library/skills",
        json={"name": "summarize", "version": "not-semver", "description": "x"},
        headers=AUTH,
    )
    assert r.status_code == 400, r.text


def test_register_skill_and_upload_file(app):
    body = {
        "name": "summarize",
        "version": "1.0.0",
        "description": "Summarize text",
        "summary": "One-paragraph summary",
    }
    r = app.post("/v1/library/skills", json=body, headers=AUTH)
    assert r.status_code == 201, r.text
    reg = r.json()
    assert reg["name"] == "summarize"
    assert reg["version"] == "1.0.0"
    assert "sha256_card" in reg

    skill_md = b"---\nname: summarize\n---\n# Summarize\n"
    r2 = app.post(
        "/v1/library/skills/summarize@1.0.0/files/SKILL.md",
        content=skill_md,
        headers={
            **AUTH,
            "X-File-Sha256": _sha(skill_md),
            "Content-Type": "text/markdown",
        },
    )
    assert r2.status_code == 201, r2.text
    up = r2.json()
    assert up["sha256"] == _sha(skill_md)
    assert up["size_bytes"] == len(skill_md)


def test_register_same_skill_twice_is_409(app):
    body = {
        "name": "summarize",
        "version": "1.0.0",
        "description": "Summarize text",
    }
    r1 = app.post("/v1/library/skills", json=body, headers=AUTH)
    assert r1.status_code == 201, r1.text
    r2 = app.post("/v1/library/skills", json=body, headers=AUTH)
    assert r2.status_code == 409, r2.text


def test_list_skills_after_register(app):
    r0 = app.post(
        "/v1/library/skills",
        json={"name": "summarize", "version": "1.0.0", "description": "x"},
        headers=AUTH,
    )
    assert r0.status_code == 201, r0.text
    r = app.get("/v1/library/skills")
    assert r.status_code == 200, r.text
    names = [s["name"] for s in r.json()["skills"]]
    assert "summarize" in names


def test_get_skill_with_version_returns_files(app):
    body = {
        "name": "summarize",
        "version": "1.0.0",
        "description": "Summarize text",
    }
    app.post("/v1/library/skills", json=body, headers=AUTH)
    payload = b"hello world"
    app.post(
        "/v1/library/skills/summarize@1.0.0/files/SKILL.md",
        content=payload,
        headers={
            **AUTH,
            "X-File-Sha256": _sha(payload),
            "Content-Type": "text/markdown",
        },
    )
    r = app.get("/v1/library/skills/summarize@1.0.0")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["name"] == "summarize"
    assert data["version"] == "1.0.0"
    assert len(data["files"]) == 1
    assert data["files"][0]["filename"] == "SKILL.md"
    assert data["files"][0]["size_bytes"] == len(payload)


def test_download_file_with_correct_sha(app):
    body = {"name": "summarize", "version": "1.0.0", "description": "x"}
    app.post("/v1/library/skills", json=body, headers=AUTH)
    payload = b"file contents"
    app.post(
        "/v1/library/skills/summarize@1.0.0/files/README.md",
        content=payload,
        headers={
            **AUTH,
            "X-File-Sha256": _sha(payload),
            "Content-Type": "text/markdown",
        },
    )
    r = app.get("/v1/library/skills/summarize@1.0.0/files/README.md")
    assert r.status_code == 200, r.text
    assert r.content == payload
    assert r.headers["content-type"].startswith("text/markdown")


def test_upload_sha_mismatch_rejected(app):
    app.post(
        "/v1/library/skills",
        json={"name": "demo", "version": "1.0.0", "description": "x"},
        headers=AUTH,
    )
    r = app.post(
        "/v1/library/skills/demo@1.0.0/files/f.bin",
        content=b"abc",
        headers={**AUTH, "X-File-Sha256": "0" * 64},
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "sha256_mismatch"


def test_upload_to_unregistered_skill_is_404(app):
    r = app.post(
        "/v1/library/skills/missing@1.0.0/files/f.bin",
        content=b"abc",
        headers={**AUTH, "X-File-Sha256": _sha(b"abc")},
    )
    assert r.status_code == 404, r.text


def test_delete_card_removes_files(app):
    app.post(
        "/v1/library/skills",
        json={"name": "demo", "version": "1.0.0", "description": "x"},
        headers=AUTH,
    )
    app.post(
        "/v1/library/skills/demo@1.0.0/files/f.bin",
        content=b"abc",
        headers={**AUTH, "X-File-Sha256": _sha(b"abc")},
    )
    r = app.delete("/v1/library/skills/demo@1.0.0", headers=AUTH)
    assert r.status_code == 204, r.text
    listing = app.get("/v1/library/skills/demo@1.0.0")
    assert listing.status_code == 404, listing.text


def test_sync_to_broker_requires_config(app):
    app.post(
        "/v1/library/skills",
        json={"name": "demo", "version": "1.0.0", "description": "x"},
        headers=AUTH,
    )
    r = app.post("/v1/library/skills/demo@1.0.0/sync-to-broker", headers=AUTH)
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["code"] == "broker_forwarding_not_configured"