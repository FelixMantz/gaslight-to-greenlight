from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from g2g.planner import assign_histories, select_specificity
from g2g.util import digest, read_jsonl, utcnow, write_json


def prepare_assignments(cases_path: Path, gold_path: Path, scripts_path: Path, seed: int, destination: Path):
    cases = read_jsonl(cases_path)
    gold = {g["case_id"]: g for g in read_jsonl(gold_path)}
    scripts = json.loads(scripts_path.read_text(encoding="utf-8"))
    script_map = {s["pair_id"]: s for s in scripts}
    if len(script_map) != 60:
        raise ValueError("60 distinct reviewed pairs required")
    for s in scripts:
        if not s.get("reviewed"):
            raise ValueError("Review scripts before assignment")
    main = [
        {
            "case_id": c["case_id"],
            "family_id": c["family_id"],
            "source": c["source"]["type"],
            "domain": c["domain"],
            "label": gold[c["case_id"]]["label"],
        }
        for c in cases
        if c["split"] == "main"
    ]
    assigned = assign_histories(main, list(script_map), seed)
    selected = select_specificity(assigned, seed + 1)
    # Domain/source diagnostics are retained; constrained source balance is not a
    # claim of exact simultaneous domain matching when families make it infeasible.
    for row in assigned:
        row["binding"] = script_map[row["history_pair_id"]]["binding"]
    # Pilot/calibration Arm 0 still use counterbalanced preassigned identities.
    import random

    rng = random.Random(seed + 2)
    for c in cases:
        if c["split"] in ("pilot", "calibration"):
            script = scripts[rng.randrange(len(scripts))]
            assigned.append(
                {
                    "case_id": c["case_id"],
                    "family_id": c["family_id"],
                    "history_pair_id": script["pair_id"],
                    "binding": script["binding"],
                    "source": c["source"]["type"],
                    "domain": c["domain"],
                    "label": gold[c["case_id"]]["label"],
                }
            )
    write_json(destination / "assignments.json", assigned, immutable=True)
    write_json(destination / "specificity.json", [r["case_id"] for r in selected], immutable=True)
    diagnostics = {
        pair: dict(
            Counter(
                r["domain"]
                for r in assigned
                if r["history_pair_id"] == pair and r["case_id"] in {x["case_id"] for x in main}
            )
        )
        for pair in script_map
    }
    write_json(
        destination / "assignment-record.json",
        {
            "seed": seed,
            "specificity_seed": seed + 1,
            "assignment_hash": digest(assigned),
            "domain_counts_by_pair": diagnostics,
        },
        immutable=True,
    )
    return {"main_cases": 450, "specificity_cases": 150, "pairs": 60, "output": str(destination)}


def _analysis_inputs(journal, study, root):
    from g2g.freeze import verify_frozen_inputs

    entry = journal.db.execute("SELECT manifest_json FROM studies WHERE study_id=?", (study,)).fetchone()
    if not entry:
        raise ValueError("Unknown study")
    manifest = json.loads(entry[0])
    verify_frozen_inputs(root, manifest)
    requests = journal.requests(study)
    if not requests or any(r["state"] != "Collected" for r in requests):
        raise ValueError("All attempts, acceptance and charges must be reconciled")
    for observation in journal.observations(study):
        journal.verify_archive(observation)
    if not manifest["fixture_only"]:
        expected = {
            "main": 3150,
            "calibrate": 500,
            "specificity": 300,
            "state": 450,
            "histories": 480,
            "state-suffix": 90,
            "state-extract": 150,
        }
        counts = Counter(json.loads(r["request_json"])["phase"] for r in requests)
        for phase, n in expected.items():
            if counts[phase] != n:
                raise ValueError(f"Frozen {phase} collection incomplete: {counts[phase]}/{n}")
        planned = {}
        for path in (root / "runs" / study / "manifest").glob("plan-*.json"):
            for spec in json.loads(path.read_text(encoding="utf-8")):
                if spec["key"] in planned:
                    raise ValueError("Duplicate frozen plan key")
                planned[spec["key"]] = spec
        for row in requests:
            request = json.loads(row["request_json"])
            if request["phase"] in expected and row["key"] not in planned:
                raise ValueError("Collected request absent from frozen plans")
        for key in planned:
            row = journal.get(key)
            if row is None or row["state"] != "Collected" or row["study_id"] != study:
                raise ValueError("Frozen planned request missing from journal")
    if journal.db.execute(
        "SELECT 1 FROM controls WHERE key='exposure_violation' OR key LIKE ? LIMIT 1", (f"paused:{study}:%",)
    ).fetchone():
        raise ValueError("Unresolved technical/exposure pause prevents confirmatory sealing")
    threshold = journal.lock_threshold(study, None)
    return manifest, requests, threshold


def validate_analysis_seal(journal, study, root):
    manifest, requests, threshold = _analysis_inputs(journal, study, root)
    path = root / "runs" / study / "analysis-ready.json"
    if not path.exists():
        raise ValueError("Confirmatory analysis point is not frozen/completed")
    record = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "study_id": study,
        "manifest_hash": digest(manifest),
        "request_hashes": {r["key"]: r["payload_hash"] for r in requests},
        "threshold_hash": digest(threshold),
        "fixture_only": manifest["fixture_only"],
    }
    if any(record.get(key) != value for key, value in expected.items()):
        raise ValueError("Analysis seal no longer matches frozen study, requests or threshold")
    return manifest, threshold


def seal_analysis(journal, study, root):
    manifest, requests, threshold = _analysis_inputs(journal, study, root)
    path = root / "runs" / study / "analysis-ready.json"
    if path.exists():
        validate_analysis_seal(journal, study, root)
    else:
        write_json(
            path,
            {
                "study_id": study,
                "time": utcnow(),
                "manifest_hash": digest(manifest),
                "request_hashes": {r["key"]: r["payload_hash"] for r in requests},
                "threshold_hash": digest(threshold),
                "fixture_only": manifest["fixture_only"],
            },
            immutable=True,
        )
    return {"status": "analysis_point_sealed", "study_id": study, "fixture_only": manifest["fixture_only"]}


def panel_report(journal, studies, root, destination):
    from g2g.reporting import analyse

    observations, requests, gold, thresholds, models = [], [], None, {}, []
    fixture_modes = set()
    case_hash = None
    for study in studies:
        manifest, threshold = validate_analysis_seal(journal, study, root)
        config = manifest["config"]
        fixture_modes.add(manifest["fixture_only"])
        current_case_hash = manifest["files"]["cases"]["sha256"]
        if case_hash is not None and case_hash != current_case_hash:
            raise ValueError("Panel uses different reviewed visible cases")
        case_hash = current_case_hash
        local_gold = {g["case_id"]: g for g in read_jsonl(root / config["inputs"]["gold"])}
        if gold is not None and digest(gold) != digest(local_gold):
            raise ValueError("Panel uses different reviewed gold")
        gold = local_gold
        model = config["model"]["model"]
        if model in models:
            raise ValueError("Duplicate model slot")
        models.append(model)
        thresholds[model] = threshold
        requests += journal.requests(study)
        observations += journal.observations(study)
    if len(models) != 4 or len(set(studies)) != 4:
        raise ValueError("Confirmatory family requires exactly four distinct model slots")
    if len(fixture_modes) != 1:
        raise ValueError("Fixture and real study observations cannot share a confirmatory panel")
    if fixture_modes == {False}:
        for study in studies:
            entry = journal.db.execute(
                "SELECT manifest_json FROM studies WHERE study_id=?", (study,)
            ).fetchone()
            panel = json.loads(entry[0])["config"].get("panel")
            if panel is None or len(panel) != 4 or set(panel) != set(models):
                raise ValueError("Panel model IDs differ from frozen panel specification")
    result = analyse(observations, requests, gold, thresholds, models, fixture_only=(fixture_modes == {True}))
    write_json(destination, result)
    return {
        "output": str(destination),
        "models": models,
        "holm_family": 8,
        "fixture_only": fixture_modes == {True},
    }
