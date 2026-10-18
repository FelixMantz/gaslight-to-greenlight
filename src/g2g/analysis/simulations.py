from __future__ import annotations

import numpy as np

from g2g.analysis.inference import paired_estimate


def simulate_inference(
    assignment: list[dict],
    replicates=1000,
    seed=20261018,
    binary=False,
    family_sd=0.2236068,
    history_sd=0.2236068,
    residual_sd=0.9486833,
    missing_rate=0.0,
    baseline_rate=0.5,
    shared_uniform_probability=0.5,
):
    """Diagnostic at the actual frozen sparse design, including binary discordance/missingness.

    Broad scenario sweeps and scientific signoff remain a pre-freeze research task.
    """
    if not assignment or replicates < 2:
        raise ValueError("At least two replicates and a nonempty assignment required")
    if not 0 <= missing_rate < 1:
        raise ValueError("Invalid missing rate")
    if not 0 < baseline_rate < 1 or not 0 <= shared_uniform_probability <= 1:
        raise ValueError("Invalid binary baseline/discordance configuration")
    rng = np.random.default_rng(seed)
    families = sorted({r["family_id"] for r in assignment})
    histories = sorted({r["history_pair_id"] for r in assignment})
    means, variances, coverage, rejections, floors = [], [], [], [], []
    inconclusive = 0
    for _ in range(replicates):
        fs = dict(zip(families, rng.normal(0, family_sd, len(families))))
        hs = dict(zip(histories, rng.normal(0, history_sd, len(histories))))
        rows = []
        for row in assignment:
            difference = fs[row["family_id"]] + hs[row["history_pair_id"]] + rng.normal(0, residual_sd)
            if binary:
                # Symmetric arm-specific logits preserve the unconditional null,
                # while family/history effects induce correlated paired differences.
                base = np.log(baseline_rate / (1 - baseline_rate))
                p1 = 1 / (1 + np.exp(-(base - difference / 2)))
                p2 = 1 / (1 + np.exp(-(base + difference / 2)))
                u1 = rng.random()
                u2 = u1 if rng.random() < shared_uniform_probability else rng.random()
                difference = int(u2 < p2) - int(u1 < p1)
            if rng.random() >= missing_rate:
                rows.append({**row, "difference": difference})
        estimate = paired_estimate(rows)
        if estimate["status"] != "estimated":
            inconclusive += 1
            continue
        means.append(estimate["mean"])
        variances.append(estimate["variance"])
        coverage.append(estimate["ci95_marginal"][0] <= 0 <= estimate["ci95_marginal"][1])
        rejections.append(estimate["p"] < 0.05)
        floors.append(estimate["floor_used"])
    return {
        "diagnostic_only": True,
        "seed": seed,
        "replicates": replicates,
        "estimated": len(means),
        "inconclusive": inconclusive,
        "binary": binary,
        "missing_rate": missing_rate,
        "missingness_model": "MCAR",
        "baseline_rate": baseline_rate,
        "shared_uniform_probability": shared_uniform_probability,
        "variance_components": [family_sd**2, history_sd**2, residual_sd**2],
        "empirical_variance": float(np.var(means, ddof=1)) if len(means) > 1 else None,
        "mean_estimated_variance": float(np.mean(variances)) if variances else None,
        "null_coverage": float(np.mean(coverage)) if coverage else None,
        "null_rejection_rate": float(np.mean(rejections)) if rejections else None,
        "floor_frequency": float(np.mean(floors)) if floors else None,
    }
