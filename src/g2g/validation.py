from __future__ import annotations

import json
from collections import Counter, defaultdict

from g2g.models import AuditCase, GoldRecord, IdentityBinding, VisiblePacket
from g2g.planner import validate_assignments
from g2g.util import digest, read_jsonl


def validate_assignment_records(cases, gold, assignments, scripts=None, specificity=None, strict=True):
    ids = [r["case_id"] for r in assignments]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate assignment ID")
    lookup = {c.case_id: c for c in cases}
    assigned = {r["case_id"]: r for r in assignments}
    required = {c.case_id for c in cases if c.split != "state"}
    if set(assigned) != required:
        raise ValueError("Assignment membership differs from reviewed cases")
    script_map = {s["pair_id"]: s for s in scripts} if scripts is not None else None
    for cid, row in assigned.items():
        case = lookup[cid]
        if (
            row["family_id"] != case.family_id
            or row["label"] != gold[cid].label
            or row["source"] != case.source["type"]
        ):
            raise ValueError("Assignment family/class/source differs from reviewed case")
        binding = IdentityBinding.model_validate(row["binding"])
        if script_map is not None:
            script = script_map.get(row["history_pair_id"])
            if script is None or binding.model_dump() != script["binding"]:
                raise ValueError("Assignment identity/pair differs from frozen actor script")
    main = [assigned[c.case_id] for c in cases if c.split == "main"]
    if main:
        validate_assignments(main)
    if specificity is not None:
        if len(specificity) != len(set(specificity)) or not set(specificity) <= {r["case_id"] for r in main}:
            raise ValueError("Specificity membership must be a unique Main subset")
        chosen = [assigned[cid] for cid in specificity]
        if strict:
            if len(chosen) != 150 or Counter(r["label"] for r in chosen) != Counter(benign=75, violating=75):
                raise ValueError("Specificity requires 150 cases with 75 of each class")
            groups = defaultdict(set)
            for row in chosen:
                groups[row["history_pair_id"]].add(row["label"])
            if len(groups) != 60 or any(labels != {"benign", "violating"} for labels in groups.values()):
                raise ValueError("Specificity requires both classes within all 60 pairs")
            if Counter(r["source"] for r in chosen) != Counter(adapted=75, synthetic=75):
                raise ValueError("Specificity requires 75 cases from each source")
            cells = Counter((r["label"], r["source"]) for r in chosen)
            if any(
                cells[(label, source)] not in (37, 38)
                for label in ("benign", "violating")
                for source in ("adapted", "synthetic")
            ):
                raise ValueError("Specificity class/source cells must contain 37 or 38 cases")


def validate_state_templates(templates, cases, gold):
    canonical = {c.case_id: c for c in cases if c.split == "state"}
    template_ids = [t["grant_template_id"] for t in templates]
    if len(template_ids) != len(set(template_ids)):
        raise ValueError("Duplicate state template ID")
    ids = []
    for template in templates:
        for variant in template["cases"]:
            cid = variant["case_id"]
            ids.append(cid)
            case = canonical.get(cid)
            if (
                case is None
                or template["family_id"] != case.family_id
                or variant.get("family_id", template["family_id"]) != case.family_id
            ):
                raise ValueError("State template differs from reviewed case/family membership")
            visible = VisiblePacket.model_validate(variant["visible"])
            if digest(visible.model_dump(mode="json")) != digest(case.visible.model_dump(mode="json")):
                raise ValueError("State template visible packet differs from reviewed case")
            if variant.get("gold", {}).get("label") != gold[cid].label:
                raise ValueError("State template label differs from reviewed gold")
    if len(ids) != len(set(ids)) or set(ids) != set(canonical):
        raise ValueError("State templates must cover reviewed state cases exactly once")


def validate_inputs(root, config, phase):
    if config["provider"] == "fixture" or phase == "dataset":
        return
    if phase == "histories":
        scripts = json.loads((root / config["inputs"]["scripts"]).read_text(encoding="utf-8"))
        if (
            len(scripts) != 60
            or len({s["pair_id"] for s in scripts}) != 60
            or len({s["script_id"] for s in scripts}) != 60
        ):
            raise ValueError("60 distinct reviewed actor-script pairs required")
        return
    cases = [AuditCase.model_validate(c) for c in read_jsonl(root / config["inputs"]["cases"])]
    labels = [GoldRecord.model_validate(g) for g in read_jsonl(root / config["inputs"]["gold"])]
    gold = {g.case_id: g for g in labels}
    if (
        len(gold) != len(labels)
        or len({c.case_id for c in cases}) != len(cases)
        or set(gold) != {c.case_id for c in cases}
    ):
        raise ValueError("Case/gold IDs must be a unique bijection")
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
    targets = {
        "pilot": {"benign": 25, "violating": 25},
        "main": {"benign": 150, "violating": 300},
        "calibration": {"benign": 500},
        "state": {"authorised": 75, "prohibited": 75},
    }
    if dict(Counter(gold[c.case_id].label for c in selected)) != targets[split]:
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
    if phase == "state":
        templates = json.loads((root / config["inputs"]["state_templates"]).read_text(encoding="utf-8"))
        validate_state_templates(templates, cases, gold)
    else:
        assignments = json.loads((root / config["inputs"]["assignments"]).read_text(encoding="utf-8"))
        scripts = (
            json.loads((root / config["inputs"]["scripts"]).read_text(encoding="utf-8"))
            if "scripts" in config["inputs"]
            else None
        )
        selection = (
            json.loads((root / config["inputs"]["specificity"]).read_text(encoding="utf-8"))
            if phase == "specificity"
            else None
        )
        validate_assignment_records(cases, gold, assignments, scripts, selection)
