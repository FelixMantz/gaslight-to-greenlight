from __future__ import annotations

import importlib.metadata
import json
import subprocess
from pathlib import Path

from g2g.util import digest, file_hash, source_commit


def freeze_study(root: Path, config: dict) -> dict:
    if config["provider"] != "fixture":
        result = subprocess.run(
            [
                "git",
                "diff",
                "--quiet",
                "HEAD",
                "--",
                "src",
                "prompts",
                "protocol",
                "pyproject.toml",
                "uv.lock",
            ],
            cwd=root,
        )
        if result.returncode:
            raise ValueError("Commit source/protocol changes before freezing a live study")
    files = {}
    inputs = config.get("inputs", {})
    for name, path_text in inputs.items():
        path = (root / path_text).resolve()
        if not path.is_file():
            raise ValueError(f"Missing frozen input: {name}: {path_text}")
        files[name] = {"path": str(path.relative_to(root.resolve())), "sha256": file_hash(path)}
    for directory in ("prompts", "protocol", "config"):
        for path in sorted((root / directory).rglob("*")):
            if path.is_file():
                files[str(path.relative_to(root))] = {
                    "path": str(path.relative_to(root)),
                    "sha256": file_hash(path),
                }
    for gate, entry in config.get("gates", {}).items():
        if isinstance(entry, dict) and entry.get("evidence_file"):
            path = root / entry["evidence_file"]
            files["gate:" + gate] = {"path": entry["evidence_file"], "sha256": file_hash(path)}
    packages = {
        name: importlib.metadata.version(name)
        for name in ("inspect-ai", "pydantic", "numpy", "scipy", "httpx", "PyYAML")
    }
    return {
        "schema_version": 1,
        "architecture": 6,
        "source_commit": source_commit(root),
        "config": config,
        "files": files,
        "packages": packages,
        "fixture_only": config["provider"] == "fixture",
    }


def verify_frozen_inputs(root: Path, manifest: dict):
    if source_commit(root) != manifest["source_commit"]:
        raise ValueError("Source commit differs from frozen study; use original checkout")
    for item in manifest["files"].values():
        if file_hash(root / item["path"]) != item["sha256"]:
            raise ValueError(f"Frozen input modified: {item['path']}")
    for name, version in manifest["packages"].items():
        if importlib.metadata.version(name) != version:
            raise ValueError(f"Package version differs from freeze: {name}")


def readiness(root: Path, config: dict, phase: str):
    if config["provider"] == "fixture":
        return
    if __import__("os").environ.get("GITHUB_ACTIONS") or __import__("os").environ.get("CODESPACES"):
        raise ValueError("Paid work is supported only on the sole local laptop runner")
    gates = config.get("gates", {})
    required = [
        "laptop_runtime_verified",
        "endpoint_verified",
        "prices_verified",
        "finite_cost_bound_verified",
    ]
    if phase not in ("dataset", "pilot"):
        required += [
            "pilot_passed",
            "protocol_reviewed",
            "replay_probe_passed",
            "analysis_simulations_reviewed",
            "remaining_workload_budget_reviewed",
        ]
    if phase in ("calibrate", "main", "specificity", "state"):
        required += ["dataset_reviewed", "source_allocation_reviewed", "scripts_reviewed"]
    for gate in required:
        entry = gates.get(gate)
        if not isinstance(entry, dict) or not entry.get("passed") or not entry.get("evidence_file"):
            raise ValueError(f"Live gate incomplete: {gate}")
        evidence = json.loads((root / entry["evidence_file"]).read_text())
        if not evidence.get("passed") or evidence.get("config_hash") != digest(config["model"]):
            raise ValueError(f"Gate evidence missing or stale: {gate}")
    if not config["model"].get("model") or not config["price"].get("version"):
        raise ValueError("Exact model and dated price snapshot required")
