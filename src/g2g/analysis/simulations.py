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
):
    """Diagnostic at the actual frozen sparse design, including binary discordance/missingness.

    Broad scenario sweeps and scientific signoff remain a pre-freeze research task.
    """
    if not assignment or replicates < 2:
        raise ValueError("At least two replicates and a nonempty assignment required")
    if not 0 <= missing_rate < 1:
        raise ValueError("Invalid missing rate")
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
                # Paired discordance with shared latent heterogeneity and independent sign.
                probability = 1 / (1 + np.exp(-difference))
                difference = int(rng.random() < probability) - int(rng.random() < probability)
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
        "variance_components": [family_sd**2, history_sd**2, residual_sd**2],
        "empirical_variance": float(np.var(means, ddof=1)) if len(means) > 1 else None,
        "mean_estimated_variance": float(np.mean(variances)) if variances else None,
        "null_coverage": float(np.mean(coverage)) if coverage else None,
        "null_rejection_rate": float(np.mean(rejections)) if rejections else None,
        "floor_frequency": float(np.mean(floors)) if floors else None,
    }
