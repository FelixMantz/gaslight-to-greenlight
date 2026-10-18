from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from g2g.planner import assign_histories, select_specificity
from g2g.util import digest, read_jsonl, utcnow, write_json


def prepare_assignments(cases_path: Path, gold_path: Path, scripts_path: Path, seed: int, destination: Path):
    cases = read_jsonl(cases_path)
    gold = {g["case_id"]: g for g in read_jsonl(gold_path)}
    scripts = json.loads(scripts_path.read_text())
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


def seal_analysis(journal, study, root):
    entry = journal.db.execute("SELECT manifest_json FROM studies WHERE study_id=?", (study,)).fetchone()
    if not entry:
        raise ValueError("Unknown study")
    manifest = json.loads(entry[0])
    requests = journal.requests(study)
    if not requests or any(r["state"] != "Collected" for r in requests):
        raise ValueError("All attempts, acceptance and charges must be reconciled")
    plans = root / "runs" / study / "manifest"
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
        for path in plans.glob("plan-*.json"):
            for spec in json.loads(path.read_text()):
                row = journal.get(spec["key"])
                if not row or row["state"] != "Collected":
                    raise ValueError("Frozen planned request missing from journal")
        if journal.db.execute(
            "SELECT 1 FROM controls WHERE key='exposure_violation' OR key LIKE ? LIMIT 1",
            (f"paused:{study}:%",),
        ).fetchone():
            raise ValueError("Unresolved technical/exposure pause prevents confirmatory sealing")
    for observation in journal.observations(study):
        journal.verify_archive(observation)
    record = {
        "study_id": study,
        "time": utcnow(),
        "manifest_hash": digest(manifest),
        "request_hashes": {r["key"]: r["payload_hash"] for r in requests},
        "threshold_hash": digest(json.loads((root / "runs" / study / "threshold.json").read_text())),
        "fixture_only": manifest["fixture_only"],
    }
    write_json(root / "runs" / study / "analysis-ready.json", record, immutable=True)
    return {"status": "analysis_point_sealed", "study_id": study, "fixture_only": manifest["fixture_only"]}


def panel_report(journal, studies, root, destination):
    from g2g.reporting import analyse

    observations, requests, gold, thresholds, models = [], [], None, {}, []
    for study in studies:
        entry = journal.db.execute("SELECT manifest_json FROM studies WHERE study_id=?", (study,)).fetchone()
        if not entry:
            raise ValueError("Unknown study")
        manifest = json.loads(entry[0])
        config = manifest["config"]
        if not (root / "runs" / study / "analysis-ready.json").exists():
            raise ValueError("Seal every model at the frozen analysis point first")
        local_gold = {g["case_id"]: g for g in read_jsonl(root / config["inputs"]["gold"])}
        if gold is not None and digest(gold) != digest(local_gold):
            raise ValueError("Panel uses different reviewed gold")
        gold = local_gold
        model = config["model"]["model"]
        if model in models:
            raise ValueError("Duplicate model slot")
        models.append(model)
        thresholds[model] = json.loads((root / "runs" / study / "threshold.json").read_text())
        requests += journal.requests(study)
        for observation in journal.observations(study):
            journal.verify_archive(observation)
            observations.append(observation)
    if len(models) != 4:
        raise ValueError("Confirmatory family requires exactly four verified model slots")
    result = analyse(observations, requests, gold, thresholds, models)
    write_json(destination, result)
    return {"output": str(destination), "models": models, "holm_family": 8}
