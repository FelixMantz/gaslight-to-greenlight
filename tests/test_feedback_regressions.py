import json

import pytest

from g2g.analysis.calibration import fit_threshold
from g2g.reporting import analyse
from g2g.util import canonical


def test_tied_calibration_reports_resolution_without_changing_policy():
    rows = [
        {"case_id": str(i), "family_id": str(i // 2), "split": "calibration", "arm": 0,
         "label": "benign", "status": "valid", "source": "synthetic", "score": 100}
        for i in range(5)
    ]
    result = fit_threshold(rows, {"synthetic": 1}, {"synthetic": 5})
    assert result["tau"] == 101 and result["comparator"] == ">="
    diag = result["diagnostics"]
    assert diag["boundary_score_mass"] == pytest.approx(1)
    assert diag["unused_empirical_allowance"] == pytest.approx(.05)
    assert diag["weighted_row_resolution_by_source"] == {"synthetic": .2}
    assert len(diag["warnings"]) == 3
    assert not diag["population_fpr_guarantee"]
    canonical(result)  # No non-finite numbers in saved diagnostics.


def analysis_inputs():
    request = {"key": "one", "purpose": "decision", "model_id": "m", "phase": "main",
               "metadata": {"case_id": "c", "family_id": "f", "arm": 0, "source": "synthetic"}}
    requests = [{"key": "one", "request_json": json.dumps(request)},
                {"key": "two", "request_json": json.dumps({**request, "key": "two"})}]
    observations = [{"key": "one", "final_text": "broken", "status": "success"}]
    return observations, requests, {"c": {"label": "benign"}}, {"m": {"tau": 50, "comparator": ">="}}, ["m"]


def test_reporting_accounts_for_invalid_and_missing_decisions():
    result = analyse(*analysis_inputs())
    assert result["decision_coverage"] == {
        "registered": 2, "observed": 1, "valid": 0, "invalid_escalated": 1,
        "missing": ["two"], "anomaly_reasons": {"delimiter_structure": 1},
    }
    assert next(iter(result["utility_and_deployed_policy"].values()))["flag_or_escalate"] == 1
    assert result["primary"]["m:score"]["valid_pair_coverage"] is None
    canonical(result)


@pytest.mark.parametrize("fault", ["request_duplicate", "observation_duplicate", "unknown", "threshold", "panel"])
def test_reporting_refuses_silent_loss_or_overwrite(fault):
    observations, requests, gold, thresholds, panel = analysis_inputs()
    if fault == "request_duplicate":
        requests.append(requests[0])
    elif fault == "observation_duplicate":
        observations.append(observations[0])
    elif fault == "unknown":
        observations[0]["key"] = "unknown"
    elif fault == "threshold":
        thresholds = {}
    else:
        panel = []
    with pytest.raises(ValueError):
        analyse(observations, requests, gold, thresholds, panel)


def test_family_threshold_sensitivity_preserves_synthetic_only_rule():
    from g2g.analysis.calibration import threshold_family_sensitivity

    rows = [
        {"case_id": str(i), "family_id": str(i // 2), "split": "calibration", "arm": 0,
         "label": "benign", "status": "valid", "source": "synthetic", "score": 5}
        for i in range(20)
    ]
    result = threshold_family_sensitivity(rows, replicates=10, weights={"synthetic": 1})
    assert result["thresholds"] == [6] * 10
    assert result["weights"] == {"synthetic": 1}
    assert not result["changes_operative_threshold"]
