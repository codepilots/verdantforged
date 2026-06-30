from contextlib import asynccontextmanager
import os

from fastapi import FastAPI

from skill_library.config import Config
from skill_library.db import connect, init_db
from skill_library.routes.healthz import router as healthz_router
from skill_library.routes.skills import router as skills_router


def create_app() -> FastAPI:
    cfg = Config.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        os.makedirs(os.path.dirname(cfg.db_path) or ".", exist_ok=True)
        conn = connect(cfg.db_path)
        init_db(conn)
        conn.close()
        os.makedirs(cfg.files_dir, exist_ok=True)
        app.state.config = cfg
        yield

    app = FastAPI(
        title="VerdantForged Skill Library",
        description="Catalog + registry for NemoClaw-compatible skills.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.config = cfg
    app.include_router(healthz_router)
    app.include_router(skills_router)
    return app


app = create_app()
