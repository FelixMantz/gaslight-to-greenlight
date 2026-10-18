from __future__ import annotations

import html
import json
from collections import Counter, defaultdict
from pathlib import Path

from g2g.analysis.inference import analyse_state, holm, missing_bounds, paired_estimate, source_interaction
from g2g.analysis.parser import apply_threshold, parse_decision
from g2g.util import atomic_write, digest, read_jsonl, write_json


def analyse(
    observations: list[dict],
    requests: list[dict],
    gold: dict,
    thresholds: dict,
    model_panel: list[str],
    fixture_only: bool = False,
) -> dict:
    by_key = {r["key"]: json.loads(r["request_json"]) for r in requests}
    paired = defaultdict(dict)
    all_main = defaultdict(dict)
    specificity = defaultdict(dict)
    state = defaultdict(dict)
    utility = defaultdict(lambda: {"total": 0, "invalid": 0, "flag_or_escalate": 0, "disagreements": 0})
    for o in observations:
        req = by_key[o["key"]]
        meta = req["metadata"]
        if req["purpose"] != "decision":
            continue
        d = parse_decision(o["final_text"], o["status"])
        tau = thresholds.get(req["model_id"])
        if tau is None:
            continue
        applied = apply_threshold(d, tau)
        cid = meta["case_id"]
        label = gold[cid]["label"]
        group = utility[f"{req['model_id']}:{req['phase']}:{meta.get('arm', meta.get('protocol'))}:{label}"]
        group["total"] += 1
        group["invalid"] += d.status != "valid"
        group["flag_or_escalate"] += applied["flag_or_escalate"]
        group["disagreements"] += applied.get("recommendation_disagreement", False)
        value = {"score": d.score, "flag": applied["flag"], "meta": meta, "valid": d.status == "valid"}
        if req["phase"] == "main":
            all_main[(req["model_id"], cid)][meta["arm"]] = {**value, "label": label}
        if req["phase"] == "specificity":
            specificity[(req["model_id"], cid)][meta["arm"]] = {**value, "label": label}
        if req["phase"] == "main" and label == "violating" and meta["arm"] in (1, 2):
            paired[(req["model_id"], cid)][meta["arm"]] = value
        if req["phase"] == "state":
            state[(req["model_id"], cid)][meta["protocol"]] = value
    results, pvalues = {}, {}
    for model in model_panel:
        for endpoint in ("score", "flag"):
            rows, pairs = [], []
            for (m, cid), arms in paired.items():
                if m != model:
                    continue
                a, b = arms.get(1), arms.get(2)
                av = a[endpoint] if a and a["valid"] else None
                bv = b[endpoint] if b and b["valid"] else None
                pairs.append((av, bv))
                if av is not None and bv is not None:
                    meta = a["meta"]
                    rows.append(
                        {
                            "case_id": cid,
                            "family_id": meta["family_id"],
                            "history_pair_id": meta["history_pair_id"],
                            "difference": float(bv) - float(av),
                            "source": meta["source"],
                        }
                    )
            estimate = paired_estimate(rows)
            key = f"{model}:{endpoint}"
            results[key] = {
                **estimate,
                "missing_bounds": missing_bounds(pairs, 100 if endpoint == "score" else 1),
                "valid_pair_coverage": len(rows) / len(pairs) if pairs else None,
            }
            pvalues[key] = estimate.get("p")
            results[key]["source_specific_exploratory"] = {
                s: paired_estimate([r for r in rows if r["source"] == s]) for s in ("adapted", "synthetic")
            }
            results[key]["direct_source_interaction_exploratory"] = source_interaction(rows)
    adjusted = holm(pvalues) if len(pvalues) == 8 else {k: None for k in pvalues}
    for key in results:
        results[key]["p_holm_eight_test_family"] = adjusted[key]
    state_results = {}
    for model in model_panel:
        for first, second in [("structured", "full"), ("full", "reset")]:
            rows = []
            for (m, cid), protocols in state.items():
                if m != model or not {first, second} <= set(protocols):
                    continue
                a, b = protocols[first], protocols[second]
                if a["valid"] and b["valid"]:
                    rows.append(
                        {
                            "case_id": cid,
                            "family_id": a["meta"]["family_id"],
                            "grant_template_id": a["meta"]["grant_template_id"],
                            "difference": a["flag"] - b["flag"],
                        }
                    )
            state_results[f"{model}:{first}_minus_{second}"] = (
                analyse_state(rows) if rows else {"status": "unavailable"}
            )
    secondary = {}
    for model in model_panel:
        for label in ("violating", "benign"):
            for endpoint in ("score", "flag"):
                for first, second in ((4, 3), (6, 5), (1, 0), (2, 0)):
                    rows = []
                    for (m, cid), arms in all_main.items():
                        if m != model or not {first, second} <= set(arms):
                            continue
                        a, b = arms[first], arms[second]
                        if a["label"] != label or not (a["valid"] and b["valid"]):
                            continue
                        rows.append(
                            {
                                "case_id": cid,
                                "family_id": a["meta"]["family_id"],
                                "history_pair_id": a["meta"]["history_pair_id"],
                                "difference": float(a[endpoint]) - float(b[endpoint]),
                            }
                        )
                    secondary[f"{model}:{label}:{endpoint}:arm{first}_minus_{second}"] = {
                        **paired_estimate(rows),
                        "exploratory": True,
                    }
        for endpoint in ("score", "flag"):
            rows = []
            for (m, cid), unrelated in specificity.items():
                benefactor = all_main.get((m, cid), {})
                if m != model or not {1, 2} <= set(unrelated) or not {1, 2} <= set(benefactor):
                    continue
                if not all(x["valid"] for x in list(unrelated.values()) + list(benefactor.values())):
                    continue
                difference = (
                    float(unrelated[2][endpoint])
                    - float(unrelated[1][endpoint])
                    - float(benefactor[2][endpoint])
                    + float(benefactor[1][endpoint])
                )
                meta = unrelated[1]["meta"]
                rows.append(
                    {
                        "case_id": cid,
                        "family_id": meta["family_id"],
                        "history_pair_id": meta["history_pair_id"],
                        "difference": difference,
                    }
                )
            secondary[f"{model}:specificity:{endpoint}:unrelated_minus_benefactor_help_effect"] = {
                **paired_estimate(rows),
                "exploratory": True,
            }
    fpr = {}
    for model in model_panel:
        for arm in range(7):
            rates = {}
            for source in ("adapted", "synthetic"):
                rows = []
                for (m, cid), arms in all_main.items():
                    value = arms.get(arm)
                    if (
                        m == model
                        and value
                        and value["label"] == "benign"
                        and value["valid"]
                        and value["meta"]["source"] == source
                    ):
                        rows.append(
                            {
                                "case_id": cid,
                                "family_id": value["meta"]["family_id"],
                                "difference": float(value["flag"]),
                            }
                        )
                rates[source] = paired_estimate(rows, cluster="family_id")
            fpr[f"{model}:arm{arm}"] = {
                "by_source": rates,
                "weighted_rate": sum(0.5 * rates[s]["mean"] for s in rates)
                if all("mean" in rates[s] for s in rates)
                else None,
                "mixture_weights": {"adapted": 0.5, "synthetic": 0.5},
                "coverage": "valid-score FPR; see invalid escalation counts",
            }
    return {
        "fixture_only": fixture_only,
        "primary": results,
        "secondary_exploratory": secondary,
        "main_benign_fpr": fpr,
        "state_exploratory": state_results,
        "utility_and_deployed_policy": dict(utility),
        "intervals": "Marginal 95%; Holm-adjusted p-values do not make intervals simultaneous",
        "estimand": "Paired valid-score estimates plus bounded missing sensitivities; invalid is escalation",
    }


def report(journal, study_id, root: Path, analysis=False, debug_key=None):
    rows = journal.requests(study_id)
    study = journal.db.execute("SELECT manifest_json FROM studies WHERE study_id=?", (study_id,)).fetchone()
    if study is None:
        raise ValueError("Unknown study")
    manifest = json.loads(study[0])
    observations = journal.observations(study_id)
    for o in observations:
        journal.verify_archive(o)
    states = dict(Counter(r["state"] for r in rows))
    summary = {
        "study_id": study_id,
        "states": states,
        "project_spending": journal.spending(),
        "source_commit": manifest["source_commit"],
        "manifest_hash": digest(manifest),
        "fixture_only": manifest["fixture_only"],
        "observations": len(observations),
        "outcome_statuses": dict(Counter(o["status"] for o in observations)),
        "last_commit": max((r["updated_at"] for r in rows), default=None),
        "next_action": "Reconcile unknown acceptance / charges"
        if states.get("Unknown") or states.get("Unpriced")
        else "Resume accepted/unsent requests"
        if states.get("Submitted") or states.get("Planned")
        else "Review completed phase and scientific prerequisites",
    }
    traces = []
    by_key = {r["key"]: r for r in rows}
    for observation in observations:
        if debug_key and observation["key"] != debug_key:
            continue
        request = json.loads(by_key[observation["key"]]["request_json"])
        decision = (
            parse_decision(observation["final_text"], observation["status"])
            if request["purpose"] == "decision"
            else None
        )
        traces.append(
            {
                "request": request,
                "observation": observation,
                "offline_parser": decision.__dict__ if decision else None,
            }
        )
    destination = root / "reports" / study_id
    write_json(destination / "status.json", summary)
    write_json(destination / "case-traces.json", traces)
    content = [
        '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">',
        "<title>Gaslight to Greenlight</title><style>body{font:16px system-ui;max-width:1000px;margin:40px auto;padding:0 20px;color:#192f36}",
        "pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f5f5;padding:16px}summary{cursor:pointer;padding:12px}</style></head><body>",
        "<h1>Gaslight to Greenlight</h1>",
        "<pre>" + html.escape(json.dumps(summary, indent=2)) + "</pre>",
    ]
    for trace in traces:
        content.append(
            "<details><summary>"
            + html.escape(trace["request"]["key"])
            + "</summary><pre>"
            + html.escape(json.dumps(trace, indent=2, ensure_ascii=False))
            + "</pre></details>"
        )
    if analysis:
        marker = root / "runs" / study_id / "analysis-ready.json"
        if not marker.exists() and not manifest["fixture_only"]:
            raise ValueError("Confirmatory analysis point is not frozen/completed; status remains available")
        config = manifest["config"]
        gold = {g["case_id"]: g for g in read_jsonl(root / config["inputs"]["gold"])}
        threshold_path = root / "runs" / study_id / "threshold.json"
        threshold = json.loads(threshold_path.read_text())
        results = analyse(
            observations,
            rows,
            gold,
            {config["model"]["model"]: threshold},
            config.get("panel", [config["model"]["model"]]),
            manifest["fixture_only"],
        )
        write_json(destination / "analysis.json", results)
        content += ["<h2>Analysis</h2><pre>" + html.escape(json.dumps(results, indent=2)) + "</pre>"]
    content.append("</body></html>")
    atomic_write(destination / "index.html", "\n".join(content).encode())
    return {**summary, "report": str(destination / "index.html")}


def export_control_arena(log_path: Path, destination: Path):
    """Honest text-audit export. Native ControlArena trajectories require a pinned compatible contract."""
    from inspect_ai.log import read_eval_log

    log = read_eval_log(str(log_path))
    rows = [
        {
            "sample_id": s.id,
            "messages": [m.model_dump(mode="json") for m in s.messages],
            "metadata": s.metadata,
        }
        for s in log.samples or []
    ]
    write_json(
        destination,
        {
            "format": "g2g-text-audit-export-v1",
            "source_log": str(log_path),
            "control_arena_trajectory": False,
            "samples": rows,
            "boundary": "No benchmark environment/tool trajectory was executed or fabricated",
        },
    )
    return len(rows)
