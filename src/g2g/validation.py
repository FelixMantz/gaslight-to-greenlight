from __future__ import annotations

from collections import Counter, defaultdict

from g2g.models import AuditCase, GoldRecord
from g2g.util import read_jsonl


def validate_inputs(root, config, phase):
    if config["provider"] == "fixture" or phase in ("dataset", "histories"):
        return
    cases = [AuditCase.model_validate(c) for c in read_jsonl(root / config["inputs"]["cases"])]
    labels = [GoldRecord.model_validate(g) for g in read_jsonl(root / config["inputs"]["gold"])]
    gold = {g.case_id: g for g in labels}
    if len(gold) != len(labels) or len({c.case_id for c in cases}) != len(cases):
        raise ValueError("Duplicate case/gold IDs")
    families, tasks = defaultdict(set), defaultdict(set)
    for case in cases:
        families[case.family_id].add(case.split)
        if case.upstream_task_id:
            tasks[case.upstream_task_id].add(case.split)
        if case.visible.actor_id != "{{ACTOR_ID}}":
            raise ValueError("Unbound case templates must use the allowlisted actor placeholder")
        g = gold[case.case_id]
        if g.review_status not in ("accepted", "adjudicated") or g.ambiguities:
            raise ValueError("Unresolved gold/review cannot enter inference")
        if not set(g.evidence_ids) <= {e.id for e in case.visible.evidence}:
            raise ValueError("Gold references missing visible evidence")
    if any(len(v) > 1 for v in families.values()) or any(len(v) > 1 for v in tasks.values()):
        raise ValueError("Semantic family or upstream task leaks across splits")
    split = {"calibrate": "calibration", "specificity": "main"}.get(phase, phase)
    selected = [c for c in cases if c.split == split]
    expected = {"pilot": 50, "calibration": 500, "main": 450, "state": 150}[split]
    if len(selected) != expected:
        raise ValueError(f"{split} requires {expected} reviewed cases")
    counts = Counter(gold[c.case_id].label for c in selected)
    targets = {
        "pilot": {"benign": 25, "violating": 25},
        "main": {"benign": 150, "violating": 300},
        "calibration": {"benign": 500},
        "state": {"authorised": 75, "prohibited": 75},
    }
    if dict(counts) != targets[split]:
        raise ValueError("Class allocation differs from frozen design")
    if split == "main":
        cells = Counter((c.source["type"], gold[c.case_id].label) for c in selected)
        if cells != Counter(
            {
                ("adapted", "violating"): 150,
                ("synthetic", "violating"): 150,
                ("adapted", "benign"): 75,
                ("synthetic", "benign"): 75,
            }
        ):
            raise ValueError("Main source x class allocation differs from design")
    if split == "pilot" and Counter(c.source["type"] for c in selected) != Counter(adapted=25, synthetic=25):
        raise ValueError("Pilot source allocation must be 25 adapted / 25 synthetic")
    if split == "calibration":
        if dict(Counter(c.source["type"] for c in selected)) != config["calibration"]["expected_counts"]:
            raise ValueError("Calibration source allocation differs from frozen plan")
        cap = config["calibration"].get("adapted_family_cap", 2)
        if any(
            n > cap for n in Counter(c.family_id for c in selected if c.source["type"] == "adapted").values()
        ):
            raise ValueError("Calibration adapted-family concentration exceeds frozen cap")
