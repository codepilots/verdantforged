"""Liveness + readiness probe for the skill-library service.

The handler reads the live Config from app.state.config (populated by the
T7 app factory; for T6 we set it directly in the test fixture) and
performs cheap probes against the SQLite DB and the on-disk files dir.
It deliberately avoids taking a write lock — this endpoint must answer
even if the DB is busy, so the broker's health-check loop doesn't get
spurious failures during heavy ingest.
"""
import os

from fastapi import APIRouter, Request

from skill_library.db import connect

router = APIRouter()


@router.get("/healthz")
def healthz(request: Request) -> dict:
    cfg = request.app.state.config

    db_ok = True
    try:
        with connect(cfg.db_path) as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception:
        db_ok = False

    efs_ok = True
    try:
        os.stat(cfg.files_dir)
    except Exception:
        efs_ok = False

    return {
        "ok": db_ok and efs_ok,
        "db": "ok" if db_ok else "error",
        "efs": "ok" if efs_ok else "error",
    }