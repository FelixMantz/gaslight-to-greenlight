from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
from pathlib import Path

from g2g.util import digest, file_hash, source_commit

SOURCE_PATHS = ("src", "prompts", "protocol", "schemas", "pyproject.toml", "uv.lock")


def source_files(root: Path) -> list[str]:
    raw = subprocess.check_output(["git", "ls-files", "-z", "--", *SOURCE_PATHS], cwd=root)
    return sorted(p for p in raw.decode("utf-8").split("\0") if p)


def require_clean_source(root: Path):
    raw = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *SOURCE_PATHS], cwd=root
    )
    if raw.strip():
        raise ValueError("Source/protocol working tree differs from its committed version")


def freeze_study(root: Path, config: dict) -> dict:
    root = root.resolve()
    if config["provider"] != "fixture":
        require_clean_source(root)
    files = {}
    absent_inputs = {}

    def add(name, path_text, optional=False):
        path = (root / path_text).resolve()
        if root not in path.parents:
            raise ValueError(f"Frozen input must be inside project: {path_text}")
        if not path.is_file():
            if optional:
                if name != "dataset:reviews":
                    absent_inputs[name] = path.relative_to(root).as_posix()
                return
            raise ValueError(f"Missing frozen input: {name}: {path_text}")
        files[name] = {"path": path.relative_to(root).as_posix(), "sha256": file_hash(path)}

    for name, path in config.get("inputs", {}).items():
        add(name, path)
    for directory in ("prompts", "protocol", "config"):
        for path in sorted((root / directory).rglob("*")):
            if path.is_file():
                add(path.relative_to(root).as_posix(), path.relative_to(root))
    for gate, entry in config.get("gates", {}).items():
        if isinstance(entry, dict) and entry.get("evidence_file"):
            add("gate:" + gate, entry["evidence_file"])
    dataset = config.get("dataset", {})
    for name in ("registry", "candidates", "reviews", "source_manifest", "source_mappings"):
        if dataset.get(name):
            optional = (name == "reviews" and not dataset.get("freeze")) or bool(
                dataset.get("generation_stages")
            )
            add("dataset:" + name, dataset[name], optional=optional)
    for i, stage in enumerate(dataset.get("generation_stages", [])):
        add(f"dataset:jobs:{i}", stage["jobs"])
    source = {path: file_hash(root / path) for path in source_files(root)}
    packages = {
        name: importlib.metadata.version(name)
        for name in ("inspect-ai", "pydantic", "numpy", "scipy", "httpx", "PyYAML")
    }
    return {
        "schema_version": 2,
        "architecture": 6,
        "source_commit": source_commit(root),
        "source_files": source,
        "config": config,
        "files": files,
        "absent_inputs": absent_inputs,
        "packages": packages,
        "fixture_only": config["provider"] == "fixture",
    }


def verify_frozen_inputs(root: Path, manifest: dict):
    if source_commit(root) != manifest["source_commit"]:
        raise ValueError("Source commit differs from frozen study; use original checkout")
    if manifest.get("schema_version", 1) < 2:
        raise ValueError("Legacy freeze lacks working-tree hashes; re-freeze before live work")
    require_clean_source(root)
    if set(source_files(root)) != set(manifest["source_files"]):
        raise ValueError("Frozen source-file inventory changed")
    for path, sha in manifest["source_files"].items():
        if file_hash(root / path) != sha:
            raise ValueError(f"Frozen source modified: {path}")
    for path in manifest.get("absent_inputs", {}).values():
        if (root / path).exists():
            raise ValueError(f"Previously absent frozen input appeared: {path}")
    for item in manifest["files"].values():
        if file_hash(root / item["path"]) != item["sha256"]:
            raise ValueError(f"Frozen input modified: {item['path']}")
    for name, version in manifest["packages"].items():
        if importlib.metadata.version(name) != version:
            raise ValueError(f"Package version differs from freeze: {name}")


def capability_config_hash(config: dict) -> str:
    return digest({"provider": config["provider"], "model": config["model"], "price": config["price"]})


def readiness(root: Path, config: dict, phase: str):
    if config["provider"] == "fixture":
        return
    if os.environ.get("GITHUB_ACTIONS") or os.environ.get("CODESPACES"):
        raise ValueError("Paid work is supported only on the sole local laptop runner")
    from g2g.budget import validate_prices, validate_output_limits

    if config["model"].get("fixture"):
        raise ValueError("Live provider cannot use fixture model settings")
    validate_prices(config["price"])
    validate_output_limits(config["model"])
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
        if not isinstance(entry, dict) or entry.get("passed") is not True or not entry.get("evidence_file"):
            raise ValueError(f"Live gate incomplete: {gate}")
        evidence = json.loads((root / entry["evidence_file"]).read_text(encoding="utf-8"))
        if evidence.get("passed") is not True or evidence.get("config_hash") != capability_config_hash(
            config
        ):
            raise ValueError(f"Gate evidence missing or stale: {gate}")
    if not config["model"].get("model") or not config["price"].get("version"):
        raise ValueError("Exact model and dated price snapshot required")
