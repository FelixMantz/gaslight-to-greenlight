from __future__ import annotations

import re
from pathlib import Path

import httpx

from g2g.util import atomic_write, file_hash


def download_hf_files(repo: str, revision: str, files: list[dict], destination: Path) -> list[Path]:
    """Explicit selected HF dataset files only; never the upstream unpinned loader."""
    if not re.fullmatch(r"[a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+", repo) or not re.fullmatch(
        r"[0-9a-f]{40}", revision
    ):
        raise ValueError("Exact repository and immutable dataset revision required")
    output = []
    with httpx.Client(follow_redirects=True, timeout=120) as client:
        for item in files:
            relative = Path(item["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unsafe source path")
            response = client.get(
                f"https://huggingface.co/datasets/{repo}/resolve/{revision}/{relative.as_posix()}"
            )
            response.raise_for_status()
            path = destination / relative
            atomic_write(path, response.content, immutable=True)
            if file_hash(path) != item["sha256"]:
                raise ValueError("Downloaded source hash mismatch")
            output.append(path)
    return output
