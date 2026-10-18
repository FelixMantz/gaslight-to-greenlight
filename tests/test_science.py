from pathlib import Path

import numpy as np
import pytest

from g2g.analysis.calibration import fit_threshold, output_allowance
from g2g.analysis.inference import analyse_state, holm, missing_bounds, paired_estimate
from g2g.compiler import compile_audit_request, compile_identity_bindings
from g2g.models import IdentityBinding, VisiblePacket
from g2g.planner import WORKLOAD, assign_histories, select_specificity, validate_assignments
from g2g.util import read_jsonl


def test_upper_tail_weighting():
    rows = []
    for source, count in [("adapted", 50), ("synthetic", 450)]:
        for i in range(count):
            rows.append(
                {
                    "case_id": f"{source}-{i}",
                    "family_id": f"{source}-{i // 2}",
                    "split": "calibration",
                    "label": "benign",
                    "arm": 0,
                    "status": "valid",
                    "source": source,
                    "score": 100 if source == "adapted" and i < 6 else 0,
                }
            )
    result = fit_threshold(rows)
    assert result["tau"] == 101  # Six adapted rows have mass .06, not 6/500.
    assert result["weight_only_ess"] == pytest.approx(180)
    rows[0]["status"] = "invalid"
    with pytest.raises(ValueError):
        fit_threshold(rows)


def test_boundary_and_output_bounds():
    rows = [
        {
            "case_id": str(i),
            "family_id": str(i),
            "split": "calibration",
            "label": "benign",
            "arm": 0,
            "status": "valid",
            "source": "synthetic",
            "score": 5,
        }
        for i in range(500)
    ]
    assert fit_threshold(rows, {"synthetic": 1})["tau"] == 6
    assert output_allowance([100, 200, 300], 10, 4096) == 2400
    with pytest.raises(ValueError):
        output_allowance([8000, 9000], 1, 4096)


def test_native_identity_and_gold_boundary():
    c = read_jsonl(Path("tests/fixtures/cases.jsonl"))[0]
    packet = VisiblePacket.model_validate(c["visible"])
    binding = IdentityBinding(benefactor_id="Avery", unrelated_actor_id="Morgan")
    a = compile_audit_request(packet, 0, binding)
    b = compile_audit_request(packet, 0, binding, swap=True)
    assert "Avery" in a["messages"][-1]["content"] and "Morgan" in b["messages"][-1]["content"]
    assert "{{" not in a["messages"][-1]["content"]
    with pytest.raises(TypeError):
        compile_audit_request(c, 0, binding)
    with pytest.raises(ValueError):
        compile_identity_bindings({"x": "{{UNKNOWN}}"}, binding)
    with pytest.raises(ValueError):
        IdentityBinding(benefactor_id="X", unrelated_actor_id="X")


def test_formula_matches_direct_cluster_sums():
    rng = np.random.default_rng(17)
    rows = [
        {
            "case_id": str(i),
            "family_id": i % 20,
            "history_pair_id": i // 20,
            "difference": float(rng.normal()),
        }
        for i in range(100)
    ]
    result = paired_estimate(rows)
    d = np.array([r["difference"] for r in rows])
    residual = d - d.mean()
    vf = 20 / 19 * sum(sum(residual[i] for i in range(100) if i % 20 == f) ** 2 for f in range(20)) / 100**2
    vh = 5 / 4 * sum(sum(residual[i] for i in range(100) if i // 20 == h) ** 2 for h in range(5)) / 100**2
    iid = sum(residual**2) / (100 * 99)
    assert result["unfloored_variance"] == pytest.approx(vf + vh - iid)
    assert result["variance"] == pytest.approx(max(vf + vh - iid, iid))
    assert result["df"] == 4 and result["cell_unique"]


def test_holm_degenerate_missing_state():
    assert (
        paired_estimate(
            [{"case_id": str(i), "family_id": i, "history_pair_id": i, "difference": 1} for i in range(4)]
        )["status"]
        == "inconclusive"
    )
    adjusted = holm({str(i): 0.001 if i == 0 else None for i in range(8)})
    assert adjusted["0"] == 0.008
    assert missing_bounds([(None, 100), (0, None)], 100) == {"n": 2, "complete": 0, "lower": 0, "upper": 100}
    rows = [
        {"case_id": str(i), "family_id": i // 10, "grant_template_id": i // 5, "difference": i % 3}
        for i in range(30)
    ]
    assert analyse_state(rows)["cluster"] == "family_id"
    assert analyse_state(rows)["df"] == 2
    assert sum(WORKLOAD.values()) == 5120


def test_full_assignment_and_specificity():
    cases = [
        {
            "case_id": str(i),
            "family_id": str(i % 90),
            "label": "violating" if i < 300 else "benign",
            "source": "adapted" if i % 2 else "synthetic",
            "domain": ["disclosure", "action_authorisation", "reporting_integrity"][i % 3],
        }
        for i in range(450)
    ]
    rows = assign_histories(cases, [str(i) for i in range(60)], 841)
    validate_assignments(rows)
    chosen = select_specificity(rows, 842)
    assert len(chosen) == 150
    assert len({r["history_pair_id"] for r in chosen}) == 60
    for pair in {r["history_pair_id"] for r in chosen}:
        assert {r["label"] for r in chosen if r["history_pair_id"] == pair} == {"benign", "violating"}
    assert sum(r["source"] == "adapted" for r in chosen) == 75
    for label in ("benign", "violating"):
        assert sum(r["label"] == label for r in chosen) == 75
        assert sum(r["label"] == label and r["source"] == "adapted" for r in chosen) in (37, 38)


def test_null_gaussian_and_binary_simulation_diagnostics():
    from g2g.analysis.simulations import simulate_inference

    rows = [
        {"case_id": str(i), "family_id": i % 60, "history_pair_id": (i // 60 + 7 * (i % 60)) % 60}
        for i in range(300)
    ]
    gaussian = simulate_inference(rows, replicates=400, seed=73)
    assert gaussian["estimated"] == 400
    assert 0.8 < gaussian["mean_estimated_variance"] / gaussian["empirical_variance"] < 1.3
    binary = simulate_inference(rows, replicates=100, seed=74, binary=True, missing_rate=0.05)
    assert binary["estimated"] == 100 and 0.8 < binary["null_coverage"] <= 1
