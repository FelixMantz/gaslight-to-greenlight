import csv
import json
from pathlib import Path

import pytest
from g2g.ingest.pipeline import generate_cases, freeze_splits, blinded_review_queue, inventory_sources
from g2g.planner import assign_splits
from g2g.util import read_jsonl


def test_review_queue_is_blinded_and_adjudication_can_correct_gold(tmp_path):
    records = read_jsonl(Path("tests/fixtures/candidates.jsonl"))[:2]
    registry = json.loads(Path("tests/fixtures/registry.json").read_text())
    cases, gold = generate_cases(records, registry)
    blinded_review_queue(cases, tmp_path / "review.md")
    text = (tmp_path / "review.md").read_text()
    assert "rationale" not in text and "family_id" not in text and "gold" not in text
    reviews = tmp_path / "reviews.csv"
    with reviews.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "case_id",
                "reviewer",
                "judgement",
                "rule_ids",
                "evidence_ids",
                "rationale",
                "ambiguous",
                "second_pass",
                "adjudicated",
            ],
        )
        writer.writeheader()
        for c in cases:
            writer.writerow(
                {
                    "case_id": c.case_id,
                    "reviewer": "human",
                    "judgement": "benign",
                    "rule_ids": "R1",
                    "evidence_ids": "E1",
                    "rationale": "Resolved explicit evidence",
                    "ambiguous": "false",
                    "second_pass": "false",
                    "adjudicated": "true",
                }
            )
    final = freeze_splits(cases, gold, registry, reviews, tmp_path / "frozen")
    assert final[0].label == "benign" and gold[0].label == "violating"
    assert (tmp_path / "frozen/gold.jsonl").exists()
    assert "gold" not in (tmp_path / "frozen/cases.jsonl").read_text()


def test_family_task_leakage_and_unpinned_inventory(tmp_path):
    families = [
        {"family_id": "a", "split": "main", "reviewed": True, "upstream_tasks": ["shared"]},
        {"family_id": "b", "split": "calibration", "reviewed": True, "upstream_tasks": ["shared"]},
    ]
    with pytest.raises(ValueError):
        assign_splits(families, 42)
    with pytest.raises(ValueError):
        inventory_sources([{"revision": "main"}], tmp_path)
