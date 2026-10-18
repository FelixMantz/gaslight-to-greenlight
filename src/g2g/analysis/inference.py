from __future__ import annotations

from collections import defaultdict
from math import sqrt

import numpy as np
from scipy.stats import t


def _cluster_variance(residuals, labels):
    groups = defaultdict(float)
    for r, label in zip(residuals, labels):
        groups[label] += float(r)
    g = len(groups)
    if g < 2:
        raise ValueError("At least two clusters required")
    return g / (g - 1) * sum(u * u for u in groups.values()) / len(residuals) ** 2, g


def paired_estimate(rows: list[dict], cluster: str | None = None) -> dict:
    """Equal-row-weight paired mean; primary family x history or state one-way."""
    if len(rows) < 2:
        return {"status": "inconclusive", "reason": "too_few_rows", "n": len(rows)}
    if len({r["case_id"] for r in rows}) != len(rows):
        raise ValueError("One paired row per case required")
    d = np.array([r["difference"] for r in rows], dtype=float)
    if not np.isfinite(d).all():
        raise ValueError("Missing/nonfinite values need explicit sensitivity, not zero imputation")
    residuals = d - d.mean()
    try:
        iid, _ = _cluster_variance(residuals, list(range(len(rows))))
        if cluster:
            variance, g = _cluster_variance(residuals, [r[cluster] for r in rows])
            df = g - 1
            diagnostics = {"cluster": cluster, "clusters": g, "unfloored_variance": variance}
        else:
            f, gf = _cluster_variance(residuals, [r["family_id"] for r in rows])
            h, gh = _cluster_variance(residuals, [r["history_pair_id"] for r in rows])
            fh, gfh = _cluster_variance(residuals, [(r["family_id"], r["history_pair_id"]) for r in rows])
            variance = f + h - fh
            df = min(gf, gh) - 1
            diagnostics = {
                "family_clusters": gf,
                "history_clusters": gh,
                "cell_clusters": gfh,
                "cell_unique": gfh == len(rows),
                "family_variance": f,
                "history_variance": h,
                "cell_variance": fh,
                "unfloored_variance": variance,
            }
        used = max(variance, iid) if not cluster else variance
    except ValueError as exc:
        return {"status": "inconclusive", "reason": str(exc), "n": len(rows), "mean": float(d.mean())}
    result = {
        "n": len(rows),
        "mean": float(d.mean()),
        "iid_variance": iid,
        "variance": used,
        "floor_used": not cluster and variance < iid,
        "df": df,
        **diagnostics,
    }
    if used <= 0 or df < 1:
        return {**result, "status": "inconclusive", "reason": "degenerate_variance"}
    se = sqrt(used)
    radius = float(t.ppf(0.975, df)) * se
    return {
        **result,
        "status": "estimated",
        "se": se,
        "ci95_marginal": [float(d.mean()) - radius, float(d.mean()) + radius],
        "p": float(2 * t.sf(abs(d.mean() / se), df)),
    }


def holm(pvalues: dict[str, float | None], family_size: int = 8) -> dict:
    if len(pvalues) != family_size:
        raise ValueError("All eight prespecified tests must be represented, including inconclusive tests")
    ordered = sorted(pvalues, key=lambda k: 1 if pvalues[k] is None else pvalues[k])
    result, previous = {}, 0.0
    for i, key in enumerate(ordered):
        p = pvalues[key]
        if p is not None and not 0 <= p <= 1:
            raise ValueError("Invalid p-value")
        previous = max(previous, (family_size - i) * (1 if p is None else p))
        result[key] = None if p is None else min(1.0, previous)
    return result


def missing_bounds(pairs: list[tuple[float | None, float | None]], maximum: float) -> dict:
    if not pairs:
        return {"n": 0, "lower": None, "upper": None}
    low = sum((0 if b is None else b) - (maximum if a is None else a) for a, b in pairs) / len(pairs)
    high = sum((maximum if b is None else b) - (0 if a is None else a) for a, b in pairs) / len(pairs)
    return {
        "n": len(pairs),
        "complete": sum(a is not None and b is not None for a, b in pairs),
        "lower": low,
        "upper": high,
    }


def analyse_state(rows: list[dict]) -> dict:
    mapping = defaultdict(set)
    for row in rows:
        mapping[row["family_id"]].add(row["grant_template_id"])
    parents = defaultdict(set)
    for row in rows:
        parents[row["grant_template_id"]].add(row["family_id"])
    if any(len(v) != 1 for v in parents.values()):
        raise ValueError("State templates must be nested in one semantic family")
    cluster = "family_id" if any(len(v) > 1 for v in mapping.values()) else "grant_template_id"
    return {**paired_estimate(rows, cluster), "exploratory": True}


def pigeonhole_sensitivity(rows: list[dict], seed: int = 17, replicates: int = 1000) -> dict:
    rng = np.random.default_rng(seed)
    families = sorted({r["family_id"] for r in rows})
    histories = sorted({r["history_pair_id"] for r in rows})
    means = []
    for _ in range(replicates):
        fw = dict(zip(families, rng.multinomial(len(families), [1 / len(families)] * len(families))))
        hw = dict(zip(histories, rng.multinomial(len(histories), [1 / len(histories)] * len(histories))))
        weights = [fw[r["family_id"]] * hw[r["history_pair_id"]] for r in rows]
        if sum(weights):
            means.append(sum(w * r["difference"] for w, r in zip(weights, rows)) / sum(weights))
    return {
        "exploratory": True,
        "method": "uncorrected_two_way_pigeonhole",
        "seed": seed,
        "replicates": len(means),
        "quantile_interval": np.quantile(means, [0.025, 0.975]).tolist(),
    }


def source_interaction(rows: list[dict]) -> dict:
    """Direct adapted-minus-synthetic paired-effect interaction, with two-way IF variance."""
    groups = {s: [r for r in rows if r["source"] == s] for s in ("adapted", "synthetic")}
    if any(len(g) < 2 for g in groups.values()):
        return {"status": "inconclusive", "reason": "too_few_source_rows", "exploratory": True}
    means = {s: sum(r["difference"] for r in g) / len(g) for s, g in groups.items()}
    n = len(rows)
    influence = [
        n
        * (1 if r["source"] == "adapted" else -1)
        * (r["difference"] - means[r["source"]])
        / len(groups[r["source"]])
        for r in rows
    ]
    # Influence values sum to zero; paired_estimate's centring leaves them unchanged.
    variance = paired_estimate([{**r, "difference": u} for r, u in zip(rows, influence)])
    contrast = means["adapted"] - means["synthetic"]
    if variance["status"] == "estimated":
        radius = float(t.ppf(0.975, variance["df"])) * variance["se"]
        variance.update(
            mean=contrast,
            ci95_marginal=[contrast - radius, contrast + radius],
            p=float(2 * t.sf(abs(contrast / variance["se"]), variance["df"])),
        )
    else:
        variance["mean"] = contrast
    return {**variance, "exploratory": True, "contrast": "adapted_minus_synthetic_effect"}


def crossed_random_intercept_sensitivity(rows: list[dict]) -> dict:
    """Gaussian REML score sensitivity. Reports singularity; never supplies a generic ESS."""
    from scipy.linalg import cho_factor, cho_solve
    from scipy.optimize import minimize

    n = len(rows)
    if n < 4:
        return {"status": "inconclusive", "reason": "too_few_rows", "exploratory": True}
    y = np.array([r["difference"] for r in rows])
    f = np.array([[a["family_id"] == b["family_id"] for b in rows] for a in rows], dtype=float)
    h = np.array([[a["history_pair_id"] == b["history_pair_id"] for b in rows] for a in rows], dtype=float)
    one = np.ones(n)

    def objective(logvar, details=False):
        v = np.exp(logvar)
        covariance = v[0] * f + v[1] * h + v[2] * np.eye(n)
        factor = cho_factor(covariance)
        vi1 = cho_solve(factor, one)
        mean = (vi1 @ y) / (one @ vi1)
        residual = y - mean
        loss = (
            2 * np.log(np.diag(factor[0])).sum() + np.log(one @ vi1) + residual @ cho_solve(factor, residual)
        )
        return (mean, float(1 / (one @ vi1)), v) if details else float(loss)

    result = minimize(objective, np.log([0.05, 0.05, max(0.01, np.var(y))]), bounds=[(-18, 10)] * 3)
    mean, variance, components = objective(result.x, True)
    return {
        "exploratory": True,
        "status": "estimated" if result.success else "fit_failed",
        "mean": float(mean),
        "variance": variance,
        "components": dict(zip(("family", "history", "residual"), map(float, components))),
        "singular": bool(np.any(components < 1e-6)),
        "optimiser_message": str(result.message),
        "assumption": "Gaussian crossed random intercepts; score sensitivity only",
    }
