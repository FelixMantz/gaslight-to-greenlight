import shutil
import subprocess
from pathlib import Path

import pytest
import httpx


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    for name in ("config", "protocol", "prompts", "tests/fixtures"):
        shutil.copytree(Path(name), root / name)
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "Fixture inputs",
        ],
        check=True,
        capture_output=True,
    )
    return root, tmp_path / "runtime"


@pytest.fixture(autouse=True)
def no_real_provider_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Offline tests may not make real HTTP calls")

    async def async_blocked(*args, **kwargs):
        raise AssertionError("Offline tests may not make real HTTP calls")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_blocked)
