from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_commit(root: Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def fsync_dir(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_write(path: Path, data: bytes, immutable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if immutable and path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"Immutable object differs: {path}")
        return
    fd, name = tempfile.mkstemp(prefix=".g2g-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        fsync_dir(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, value: Any, immutable: bool = False) -> None:
    atomic_write(path, canonical(value), immutable)


def runtime_dir() -> Path:
    default = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "g2g"
    return Path(os.environ.get("G2G_RUNTIME_DIR", default)).expanduser().resolve()


def ensure_local_runtime(runtime: Path, project: Path) -> None:
    # Fail closed on recognisable sync roots and project-contained state. Users must
    # additionally verify any organisation-specific sync roots during laptop setup.
    if runtime == project or project in runtime.parents:
        raise ValueError("Runtime must be outside the project / Dropbox backup tree")
    if any(
        any(s in p.lower() for s in ("dropbox", "onedrive", "icloud", "google drive")) for p in runtime.parts
    ):
        raise ValueError("Runtime directory appears to be synchronised")
