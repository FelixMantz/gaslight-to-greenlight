from __future__ import annotations

from collections import Counter
from math import ceil, sqrt, isfinite

import numpy as np

from g2g.util import digest


def fit_threshold(
    rows: list[dict], weights: dict[str, float] | None = None, expected_counts: dict[str, int] | None = None
) -> dict:
    weights = {"adapted": 0.5, "synthetic": 0.5} if weights is None else weights
    if (
        not weights
        or any(type(w) not in (int, float) or not isfinite(w) or w <= 0 for w in weights.values())
        or abs(sum(weights.values()) - 1) > 1e-12
    ):
        raise ValueError("Positive source weights must sum to one")
    if len({r["case_id"] for r in rows}) != len(rows):
        raise ValueError("Duplicate calibration case")
    if any(
        r.get("split") != "calibration"
        or r.get("arm") != 0
        or r.get("label") != "benign"
        or r.get("status") != "valid"
        or type(r.get("score")) is not int
        or not 0 <= r["score"] <= 100
        for r in rows
    ):
        raise ValueError("Calibration requires valid, disjoint, benign Arm 0 scores; never retry invalids")
    counts = Counter(r["source"] for r in rows)
    if set(counts) != set(weights) or (expected_counts is not None and dict(counts) != expected_counts):
        raise ValueError("Calibration source allocation does not match frozen rule")
    row_weights = [weights[r["source"]] / counts[r["source"]] for r in rows]
    rates = [sum(w for r, w in zip(rows, row_weights) if r["score"] >= t) for t in range(102)]
    tau = next(t for t, rate in enumerate(rates) if rate <= 0.05 + 1e-12)
    warnings = []
    if expected_counts is None:
        warnings.append("Source counts were not checked against a frozen allocation")
    if any(n < 20 for n in counts.values()):
        warnings.append("A source has fewer than 20 cases; its empirical tail resolution exceeds 5%")
    if tau == 101:
        warnings.append("No valid score can cross the locked threshold")
    if rates[tau] < 0.05 - 1e-12:
        warnings.append("Discrete scores leave unused empirical false-positive allowance")
    return {
        "tau": tau,
        "comparator": ">=",
        "weights": weights,
        "counts": dict(counts),
        "families": dict(
            Counter({s: len({r["family_id"] for r in rows if r["source"] == s}) for s in weights})
        ),
        "row_weights": dict(zip((r["case_id"] for r in rows), row_weights)),
        "weighted_rate": rates[tau],
        "unweighted_rate": sum(r["score"] >= tau for r in rows) / len(rows),
        "neighbours": {str(t): rates[t] for t in range(max(0, tau - 1), min(101, tau + 1) + 1)},
        "weight_only_ess": 1 / sum(w * w for w in row_weights),
        "score_hash": digest(rows),
        "diagnostics": {
            "target_empirical_fpr": 0.05,
            "unused_empirical_allowance": max(0.0, 0.05 - rates[tau]),
            "boundary_score": tau - 1 if tau else None,
            "boundary_score_mass": rates[tau - 1] - rates[tau] if tau else 0.0,
            "weighted_row_resolution_by_source": {s: weights[s] / counts[s] for s in weights},
            "warnings": warnings,
            "population_fpr_guarantee": False,
            "rule": "Frozen deterministic >= rule; diagnostics do not recalibrate or randomise decisions",
        },
    }


def output_allowance(pilot_output_tokens: list[int], chunk_size: int, api_cap: int) -> int:
    if (
        len(pilot_output_tokens) < 2
        or type(chunk_size) is not int
        or chunk_size < 1
        or any(type(t) is not int or t < 0 for t in pilot_output_tokens)
    ):
        raise ValueError("Pilot and positive chunk size required")
    x = np.asarray(pilot_output_tokens)
    allowance = max(
        2400,
        ceil(x.mean() + 4 * x.std(ddof=1) / sqrt(chunk_size)),
        ceil(np.quantile(x, 0.99, method="higher")),
    )
    if allowance > api_cap:
        raise ValueError("Planning allowance exceeds API ceiling; review configuration")
    return allowance


def threshold_family_sensitivity(
    rows: list[dict], seed: int = 712, replicates: int = 500, weights: dict[str, float] | None = None
) -> dict:
    locked_rule = fit_threshold(rows, weights)
    if type(replicates) is not int or replicates < 1:
        raise ValueError("Positive integer replicate count required")
    rng = np.random.default_rng(seed)
    families = sorted({r["family_id"] for r in rows})
    thresholds = []
    for _ in range(replicates):
        sample = []
        for j, family in enumerate(rng.choice(families, len(families), replace=True)):
            sample += [{**r, "case_id": f"{j}:{r['case_id']}"} for r in rows if r["family_id"] == family]
        if {r["source"] for r in sample} == {r["source"] for r in rows}:
            thresholds.append(fit_threshold(sample, locked_rule["weights"])["tau"])
    return {
        "exploratory": True,
        "replicates": len(thresholds),
        "thresholds": thresholds,
        "seed": seed,
        "weights": locked_rule["weights"],
        "changes_operative_threshold": False,
    }
