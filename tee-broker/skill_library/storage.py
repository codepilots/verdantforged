from pathlib import Path

def path_for_blob(files_dir, skill_name: str, version: str, filename: str) -> Path:
    return Path(files_dir) / skill_name / version / filename

def write_blob(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        import os
        os.fsync(f.fileno())
    os.replace(tmp, path)

def read_blob(path: Path) -> bytes:
    return path.read_bytes()

def delete_blob(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
