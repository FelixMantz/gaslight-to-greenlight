from __future__ import annotations

from collections import defaultdict

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

from g2g.util import digest

PHASES = ("dataset", "pilot", "histories", "calibrate", "main", "specificity", "state")
WORKLOAD = {
    "main": 3150,
    "calibrate": 500,
    "specificity": 300,
    "state_monitor": 450,
    "core_histories": 480,
    "state_suffixes": 90,
    "state_extraction": 150,
}


def assign_splits(families: list[dict], seed: int) -> list[dict]:
    if len({f["family_id"] for f in families}) != len(families):
        raise ValueError("Duplicate semantic family")
    seen = {}
    for family in families:
        if family["split"] not in {"pilot", "calibration", "main", "state"}:
            raise ValueError("Unknown split")
        for upstream in family.get("upstream_tasks", []):
            if upstream in seen and seen[upstream] != family["split"]:
                raise ValueError("Upstream task leakage across splits")
            seen[upstream] = family["split"]
        if not family.get("reviewed"):
            raise ValueError("Family registry requires review before expansion")
    rng = np.random.default_rng(seed)
    return [families[i] for i in rng.permutation(len(families))]


def _solve_binary(n, constraints, seed):
    matrix = lil_matrix((len(constraints), n), dtype=float)
    lower, upper = [], []
    for row, (indices, lo, hi) in enumerate(constraints):
        matrix[row, indices] = 1
        lower.append(lo)
        upper.append(hi)
    result = milp(
        np.random.default_rng(seed).uniform(0, 1, n),
        integrality=np.ones(n),
        bounds=Bounds(np.zeros(n), np.ones(n)),
        constraints=LinearConstraint(matrix.tocsr(), lower, upper),
        options={"time_limit": 120, "mip_rel_gap": 1.0},
    )
    if result.x is None or not result.success:
        raise ValueError("Assignment infeasible or solver limit reached; revise families before freeze")
    selected = np.rint(result.x).astype(int)
    values = matrix.tocsr() @ selected
    if any(v < lo - 1e-7 or v > hi + 1e-7 for v, lo, hi in zip(values, lower, upper)):
        raise ValueError("Invalid integer assignment")
    return selected


def assign_histories(cases: list[dict], bank: list[str], seed: int) -> list[dict]:
    if len(cases) != 450 or len(bank) != 60 or len(set(bank)) != 60:
        raise ValueError("Main requires 450 cases and 60 distinct history pairs")
    if sum(c["label"] == "violating" for c in cases) != 300:
        raise ValueError("Main class allocation must be 300/150")
    if len({c["case_id"] for c in cases}) != len(cases):
        raise ValueError("Duplicate case")
    n, pairs = len(cases), len(bank)
    constraints = []
    for i in range(n):
        constraints.append(([i * pairs + j for j in range(pairs)], 1, 1))
    for j in range(pairs):
        for label, count in [("violating", 5), ("benign", 2 + (j % 2))]:
            constraints.append(
                ([i * pairs + j for i, c in enumerate(cases) if c["label"] == label], count, count)
            )
        # At most one row per semantic family x history cell across both classes.
        for family in sorted({c["family_id"] for c in cases}):
            constraints.append(
                ([i * pairs + j for i, c in enumerate(cases) if c["family_id"] == family], 0, 1)
            )
        for domain in sorted({c.get("domain", "unspecified") for c in cases}):
            members = [i * pairs + j for i, c in enumerate(cases) if c.get("domain", "unspecified") == domain]
            total = len(members)
            constraints.append((members, total // pairs, (total + pairs - 1) // pairs))
        adapted = [i * pairs + j for i, c in enumerate(cases) if c["source"] == "adapted"]
        constraints.append((adapted, 3, 4))
    selected = _solve_binary(n * pairs, constraints, seed)
    result = [
        {**c, "history_pair_id": bank[j]}
        for i, c in enumerate(cases)
        for j in range(pairs)
        if selected[i * pairs + j]
    ]
    validate_assignments(result)
    return result


def validate_assignments(rows):
    if len({r["case_id"] for r in rows}) != len(rows):
        raise ValueError("Duplicate assignment")
    if len({(r["family_id"], r["history_pair_id"]) for r in rows}) != len(rows):
        raise ValueError("Repeated family x history cell")
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["history_pair_id"]].append(row)
    if len(rows) == 450:
        if len(grouped) != 60:
            raise ValueError("All 60 history pairs required")
        for group in grouped.values():
            if sum(r["label"] == "violating" for r in group) != 5 or sum(
                r["label"] == "benign" for r in group
            ) not in (2, 3):
                raise ValueError("Incorrect per-pair class assignment")


def select_specificity(rows: list[dict], seed: int) -> list[dict]:
    validate_assignments(rows)
    pairs = {r["history_pair_id"] for r in rows}
    if len(pairs) != 60:
        raise ValueError("Specificity requires all 60 history pairs")
    constraints = [(list(range(len(rows))), 150, 150)]
    for label in ("benign", "violating"):
        constraints.append(([i for i, r in enumerate(rows) if r["label"] == label], 75, 75))
        for pair in sorted(pairs):
            constraints.append(
                (
                    [i for i, r in enumerate(rows) if r["label"] == label and r["history_pair_id"] == pair],
                    1,
                    3,
                )
            )
        for source in ("adapted", "synthetic"):
            constraints.append(
                ([i for i, r in enumerate(rows) if r["label"] == label and r["source"] == source], 37, 38)
            )
    constraints.append(([i for i, r in enumerate(rows) if r["source"] == "adapted"], 75, 75))
    selected = _solve_binary(len(rows), constraints, seed)
    return [r for r, x in zip(rows, selected) if x]


def canonical_blocks(rows: list[dict], model: str, seed: int, phase: str = "main") -> dict[str, list[dict]]:
    """Five pairs/block; condition-prefix groups remain contiguous and seeded."""
    rng = np.random.default_rng(seed)
    pairs = sorted({r["history_pair_id"] for r in rows})
    rng.shuffle(pairs)
    groups = [pairs[i : i + 5] for i in range(0, len(pairs), 5)]
    blocks = {}
    for index, group in enumerate(groups):
        for arms in ((1, 2), (3, 4), (5, 6), (0,)):
            members = []
            for pair in group:
                order = list(arms)
                rng.shuffle(order)
                for arm in order:
                    for row in sorted(
                        (r for r in rows if r["history_pair_id"] == pair), key=lambda r: r["case_id"]
                    ):
                        members.append({**row, "model_id": model, "arm": arm, "phase": phase})
            block = f"{phase}:{model}:{index}:{'-'.join(map(str, arms))}"
            blocks[block] = members
    return blocks


def request_key(study, phase, model, case_id, condition, attempt=1):
    # Provider-safe custom ID, independent of coroutine timing and paid responses.
    return digest([study, phase, model, case_id, condition, attempt])


def plan_requests(phase: str, rows: list[dict], model: str, seed: int) -> dict:
    if phase not in PHASES:
        raise ValueError("Unknown phase")
    if phase == "main":
        validate_assignments(rows)
        return canonical_blocks(rows, model, seed, phase)
    grouped = {}
    # Other phases can provide explicit frozen membership (state dependencies,
    # turn waves and specificity pairs); never infer a constant request count.
    for row in rows:
        grouped.setdefault(row["block_id"], []).append(row)
    return grouped
