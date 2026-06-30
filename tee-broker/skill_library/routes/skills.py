"""HTTP routes for the skill-library service.

All routes are mounted under /v1/library. Write operations require an
API key presented as `Authorization: Bearer <key>`; the key is compared
with hmac.compare_digest against Config.api_key (which is itself read
from SKILL_LIBRARY_API_KEY). If api_key is empty, the write API is
disabled and we return 503 — that way a misconfigured deploy never
silently allows anonymous registration.

The router does not import or know about skill_library.app; the app
factory in T7 is responsible for assembling the FastAPI instance and
including this router. This keeps the routes testable in isolation
without depending on the app factory.
"""
import hashlib
import hmac
import json
import os
import re
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from pathlib import Path

from skill_library.db import connect, init_db
from skill_library.models import SkillCard
from skill_library.storage import (
    delete_blob,
    path_for_blob,
    read_blob,
    write_blob,
)

router = APIRouter(prefix="/v1/library")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


_SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _require_auth(request: Request) -> None:
    cfg = request.app.state.config
    if not cfg.api_key:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "skill-library write API is disabled (SKILL_LIBRARY_API_KEY not configured)",
                "code": "library_auth_not_configured",
            },
        )
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={
                "error": "missing or invalid Authorization header",
                "code": "library_auth_required",
            },
        )
    presented = auth[len("Bearer ") :].strip()
    if not hmac.compare_digest(presented, cfg.api_key):
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid library API key", "code": "library_auth_invalid"},
        )


def _conn(request: Request) -> sqlite3.Connection:
    cfg = request.app.state.config
    return connect(cfg.db_path)


def _files_for(conn: sqlite3.Connection, name: str, version: str) -> list:
    rows = conn.execute(
        "SELECT filename, sha256, size_bytes, content_type FROM skill_files "
        "WHERE name=? AND version=? ORDER BY filename",
        (name, version),
    ).fetchall()
    return [dict(r) for r in rows]


def _card_row(conn: sqlite3.Connection, name: str, version: str):
    return conn.execute(
        "SELECT name, version, description, license, summary, sha256_card, created_at "
        "FROM skills WHERE name=? AND version=?",
        (name, version),
    ).fetchone()


def _split_ref(ref: str) -> "tuple[str, str]":
    """Split `<name>@<version>` into a (name, version) tuple or 400."""
    if "@" not in ref:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "ref must be <name>@<version>",
                "code": "bad_ref",
            },
        )
    parts = ref.split("@", 1)
    return parts[0], parts[1]


# --------------------------------------------------------------------------- #
# Skill cards
# --------------------------------------------------------------------------- #


@router.post("/skills")
def register_skill(request: Request, body: dict):
    """Create a skill card. Returns 201 on success, 409 on duplicate."""
    _require_auth(request)
    try:
        card = SkillCard(**body)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail={"error": f"invalid skill card: {e}", "code": "invalid_card"},
        )

    cfg = request.app.state.config
    os.makedirs(cfg.files_dir, exist_ok=True)

    canonical = json.dumps(
        {
            "name": card.name,
            "version": card.version,
            "description": card.description,
            "license": card.license,
            "summary": card.summary,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    sha = _sha(canonical)

    with _conn(request) as conn:
        init_db(conn)
        try:
            conn.execute(
                "INSERT INTO skills "
                "(name, version, description, license, summary, sha256_card, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    card.name,
                    card.version,
                    card.description,
                    card.license,
                    card.summary,
                    sha,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": f"skill {card.name!r}@{card.version} already exists",
                    "code": "skill_already_registered",
                },
            )

    return JSONResponse(
        {"name": card.name, "version": card.version, "sha256_card": sha},
        status_code=201,
        headers={"Location": f"/v1/library/skills/{card.name}@{card.version}"},
    )


@router.get("/skills")
def list_skills(request: Request):
    """List all skill cards grouped by name+version, with file stats."""
    with _conn(request) as conn:
        rows = conn.execute(
            "SELECT s.name, s.version, s.summary, "
            "       (SELECT count(*) FROM skill_files f "
            "         WHERE f.name=s.name AND f.version=s.version) AS file_count, "
            "       (SELECT coalesce(sum(size_bytes),0) FROM skill_files f "
            "         WHERE f.name=s.name AND f.version=s.version) AS total_bytes "
            "FROM skills s "
            "ORDER BY s.name, s.version DESC"
        ).fetchall()
    return {
        "skills": [
            {
                "name": r["name"],
                "version": r["version"],
                "summary": r["summary"],
                "file_count": r["file_count"],
                "total_bytes": r["total_bytes"],
            }
            for r in rows
        ]
    }


@router.get("/skills/{ref}")
def get_skill(request: Request, ref: str):
    """Fetch one skill card. With `@<version>` returns the full card+files;
    without, returns all versions of that name."""
    if "@" not in ref:
        name = ref
        with _conn(request) as conn:
            rows = conn.execute(
                "SELECT version, description, summary, sha256_card, created_at "
                "FROM skills WHERE name=? ORDER BY version DESC",
                (name,),
            ).fetchall()
        if not rows:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": f"no skills named {name!r}",
                    "code": "skill_not_found",
                },
            )
        return {"name": name, "versions": [dict(r) for r in rows]}

    name, version = _split_ref(ref)
    with _conn(request) as conn:
        card = _card_row(conn, name, version)
        if card is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": f"skill {ref!r} not found",
                    "code": "skill_not_found",
                },
            )
        files = _files_for(conn, name, version)

    return {
        "name": card["name"],
        "version": card["version"],
        "description": card["description"],
        "license": card["license"],
        "summary": card["summary"],
        "sha256_card": card["sha256_card"],
        "created_at": card["created_at"],
        "files": files,
        "total_bytes": sum(f["size_bytes"] for f in files),
    }


@router.get("/skills/{ref}/manifest")
def manifest(request: Request, ref: str):
    """Broker-friendly manifest view: name/version/description/license/files."""
    name, version = _split_ref(ref)
    with _conn(request) as conn:
        card = _card_row(conn, name, version)
        files = _files_for(conn, name, version)
    if card is None:
        raise HTTPException(
            status_code=404,
            detail={"error": f"skill {ref!r} not found", "code": "skill_not_found"},
        )
    return {
        "name": card["name"],
        "version": card["version"],
        "description": card["description"],
        "license": card["license"],
        "files": files,
    }


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #


@router.post("/skills/{ref}/files/{filename:path}")
async def upload_file(
    request: Request,
    ref: str,
    filename: str,
    x_file_sha256: str | None = Header(default=None),
    content_type: str | None = Header(default=None),
):
    """Upload (or overwrite) a single file within a registered skill.

    Optional `X-File-Sha256` header lets the client pre-declare the
    expected sha256 of the body; we recompute on receipt and reject with
    400 + `sha256_mismatch` if they disagree. Without the header we
    still compute and store the sha256, we just don't compare.
    """
    _require_auth(request)
    name, version = _split_ref(ref)
    cfg = request.app.state.config
    body = await request.body()

    if x_file_sha256:
        if not _SHA256_HEX.match(x_file_sha256):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "X-File-Sha256 must be 64-char hex",
                    "code": "sha256_bad_format",
                },
            )
        computed = _sha(body)
        if computed.lower() != x_file_sha256.lower():
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "uploaded body sha256 does not match X-File-Sha256",
                    "code": "sha256_mismatch",
                    "registered_sha256": x_file_sha256.lower(),
                    "actual_sha256": computed,
                },
            )

    with _conn(request) as conn:
        init_db(conn)
        card = _card_row(conn, name, version)
        if card is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": f"skill {ref!r} not registered",
                    "code": "skill_not_found",
                },
            )

    blob_path = path_for_blob(cfg.files_dir, name, version, filename)
    write_blob(blob_path, body)
    computed = _sha(body)
    ctype = content_type or "application/octet-stream"

    with _conn(request) as conn:
        conn.execute(
            "INSERT INTO skill_files "
            "(name, version, filename, sha256, size_bytes, content_type, on_disk_path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(name, version, filename) DO UPDATE SET "
            "    sha256=excluded.sha256, size_bytes=excluded.size_bytes, "
            "    content_type=excluded.content_type, on_disk_path=excluded.on_disk_path, "
            "    created_at=excluded.created_at",
            (
                name,
                version,
                filename,
                computed,
                len(body),
                ctype,
                str(blob_path),
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    return JSONResponse(
        {"filename": filename, "sha256": computed, "size_bytes": len(body)},
        status_code=201,
        headers={"Location": f"/v1/library/skills/{ref}/files/{filename}"},
    )


@router.get("/skills/{ref}/files/{filename:path}")
def download_file(request: Request, ref: str, filename: str):
    """Download a single file. Content-type is whatever the upload set."""
    name, version = _split_ref(ref)
    with _conn(request) as conn:
        row = conn.execute(
            "SELECT on_disk_path, content_type FROM skill_files "
            "WHERE name=? AND version=? AND filename=?",
            (name, version, filename),
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"file {filename!r} not found",
                "code": "file_not_found",
            },
        )
    data = read_blob(Path(row["on_disk_path"]))
    return Response(content=data, media_type=row["content_type"])


# --------------------------------------------------------------------------- #
# Delete
# --------------------------------------------------------------------------- #


@router.delete("/skills/{ref}", status_code=204)
def delete_skill(request: Request, ref: str):
    """Remove a skill card and all of its on-disk blobs. Idempotent only
    in the sense that a missing card returns 404; deleting blobs is
    best-effort — if a blob file is already gone we silently continue."""
    _require_auth(request)
    name, version = _split_ref(ref)
    with _conn(request) as conn:
        init_db(conn)
        rows = conn.execute(
            "SELECT on_disk_path FROM skill_files WHERE name=? AND version=?",
            (name, version),
        ).fetchall()
        for r in rows:
            delete_blob(Path(r["on_disk_path"]))
        cur = conn.execute(
            "DELETE FROM skills WHERE name=? AND version=?", (name, version)
        )
        if cur.rowcount == 0:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": f"skill {ref!r} not found",
                    "code": "skill_not_found",
                },
            )
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# Broker forwarding (stub — actual HTTP push happens via httpx in T8+)
# --------------------------------------------------------------------------- #


@router.post("/skills/{ref}/sync-to-broker")
def sync_to_broker(request: Request, ref: str):
    """Forward a registered skill to the configured broker.

    This endpoint is *not* unit-tested in T6 — it requires live broker
    config (BROKER_BASE_URL + BROKER_SKILLS_API_KEY). We keep it here so
    the route surface area is complete; the implementation will land in
    T8 alongside the broker-daemon forwarding logic. For now we return
    503 with a clear error code when config is missing.
    """
    _require_auth(request)
    cfg = request.app.state.config
    if not cfg.broker_skills_api_key or not cfg.broker_base_url:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "sync-to-broker requires BROKER_SKILLS_API_KEY and BROKER_BASE_URL",
                "code": "broker_forwarding_not_configured",
            },
        )
    name, version = _split_ref(ref)
    with _conn(request) as conn:
        card = _card_row(conn, name, version)
        if card is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": f"skill {ref!r} not found",
                    "code": "skill_not_found",
                },
            )

    # Implementation deferred to T8. Returning 503 with a distinct code
    # keeps the route discoverable via the OpenAPI schema while making
    # it clear the feature isn't wired up yet.
    raise HTTPException(
        status_code=503,
        detail={
            "error": "sync-to-broker implementation lands in T8",
            "code": "broker_forwarding_not_implemented",
        },
    )