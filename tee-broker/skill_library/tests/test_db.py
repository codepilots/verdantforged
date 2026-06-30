import sqlite3
import skill_library.db as db

def test_init_db_creates_tables(tmp_db_path):
    conn = db.connect(str(tmp_db_path))
    db.init_db(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('skills','skill_files') ORDER BY name"
    ).fetchall()
    assert [r[0] for r in rows] == ['skill_files', 'skills']

def test_init_db_idempotent(tmp_db_path):
    conn = db.connect(str(tmp_db_path))
    db.init_db(conn)
    db.init_db(conn)  # second call must not raise
    n = conn.execute("SELECT count(*) FROM skills").fetchone()[0]
    assert n == 0
