import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    name        TEXT NOT NULL,
    version     TEXT NOT NULL,
    description TEXT NOT NULL,
    license     TEXT NOT NULL DEFAULT 'Apache-2.0',
    summary     TEXT NOT NULL DEFAULT '',
    sha256_card TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (name, version)
);

CREATE TABLE IF NOT EXISTS skill_files (
    name          TEXT NOT NULL,
    version       TEXT NOT NULL,
    filename      TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    content_type  TEXT NOT NULL DEFAULT 'application/octet-stream',
    on_disk_path  TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (name, version, filename),
    FOREIGN KEY (name, version) REFERENCES skills(name, version) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_skills_name      ON skills(name);
CREATE INDEX IF NOT EXISTS idx_skill_files_name ON skill_files(name);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
