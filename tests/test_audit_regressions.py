import asyncio
import copy
import json
import sqlite3
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml

from g2g.adapters.codecs import NativeCodec
from g2g.adapters.transport import FixtureTransport, ProviderTransport
from g2g.budget import price_usage, request_cost
from g2g.cli import execute, parser
from g2g.coordinator import Coordinator, BlockFailure
from g2g.engine import Engine
from g2g.freeze import freeze_study, verify_frozen_inputs
from g2g.journal import Journal
from g2g.models import AuditCase, GoldRecord, Request
from g2g.operations import seal_analysis, validate_analysis_seal
from g2g.reporting import analyse
from g2g.util import digest, read_jsonl, write_json
from g2g.validation import validate_assignment_records, validate_state_templates

PRICE = {"version": "test", "input": 0, "output": 0}


def request(key="a", block="b", study="s"):
    return Request(
        key=key,
        study_id=study,
        phase="pilot",
        purpose="history",
        model_id="fake",
        block_id=block,
        payload={"model": "fake", "messages": [{"role": "user", "content": key}], "max_tokens": 4096},
        config_hash="test",
        planning_microusd=1,
        maximum_microusd=2,
    )


def commit(root):
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "Frozen source",
        ],
        check=True,
        capture_output=True,
    )


@pytest.mark.parametrize("mutation", ["tracked_edit", "untracked_source"])
def test_resume_detects_worktree_edits_without_new_commit(project, mutation):
    root, _ = project
    (root / "src").mkdir()
    (root / "src/monitor.py").write_text("VALUE = 1\n", encoding="utf-8")
    commit(root)
    config = yaml.safe_load((root / "config/fixture.yaml").read_text(encoding="utf-8"))
    manifest = freeze_study(root, config)
    verify_frozen_inputs(root, manifest)
    sha = manifest["source_commit"]
    path = root / "src" / ("monitor.py" if mutation == "tracked_edit" else "untracked.py")
    path.write_text("VALUE = 2\n", encoding="utf-8")
    assert subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip() == sha
    with pytest.raises(ValueError, match="Source/protocol"):
        verify_frozen_inputs(root, manifest)


def test_generator_job_bytes_are_frozen(project):
    root, _ = project
    job = root / "jobs.jsonl"
    job.write_text('{"n":10}\n', encoding="utf-8")
    config = yaml.safe_load((root / "config/fixture.yaml").read_text(encoding="utf-8"))
    config["dataset"]["generation_stages"] = [{"prompt": "A", "jobs": "jobs.jsonl"}]
    manifest = freeze_study(root, config)
    job.write_text('{"n":20}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="Frozen input"):
        verify_frozen_inputs(root, manifest)


async def test_queued_submission_does_not_escape_snapshot_failure(tmp_path, monkeypatch):
    root = tmp_path / "p"
    root.mkdir()
    j = Journal(tmp_path / "r/journal.sqlite", root)

    class Slow(FixtureTransport):
        async def submit(self, requests):
            await asyncio.sleep(0.01)
            return await super().submit(requests)

    transport = Slow(tmp_path / "service")
    co = Coordinator(
        j, NativeCodec("fixture", {"model": "fake"}), transport, {"a": ["a"], "b": ["b"]}, PRICE, http_limit=1
    )
    original = j.snapshot_journal

    def fail():
        raise OSError("full disk")

    monkeypatch.setattr(j, "snapshot_journal", fail)
    result = await asyncio.gather(
        co.generate(request("a", "a")), co.generate(request("b", "b")), return_exceptions=True
    )
    assert all(isinstance(x, BlockFailure) for x in result)
    assert transport.submissions == 1
    assert j.get("a")["state"] == "Submitted" and j.get("b")["state"] == "Planned"
    monkeypatch.setattr(j, "snapshot_journal", original)
    await co.close()
    j.close()


async def test_late_block_member_and_failed_error_logging_never_hang(tmp_path, monkeypatch):
    root = tmp_path / "p"
    root.mkdir()
    j = Journal(tmp_path / "r/journal.sqlite", root)
    transport = FixtureTransport(tmp_path / "service")
    co = Coordinator(
        j, NativeCodec("fixture", {"model": "fake"}), transport, {"b": ["a", "late"]}, PRICE, watchdog=0.01
    )
    event = j.event

    def fail_log(study, key, function, branch, *args, **kwargs):
        if branch == "failed":
            raise sqlite3.OperationalError("disk full during failure logging")
        return event(study, key, function, branch, *args, **kwargs)

    monkeypatch.setattr(j, "event", fail_log)
    with pytest.raises(BlockFailure):
        await asyncio.wait_for(co.generate(request()), timeout=0.3)
    with pytest.raises(BlockFailure):
        await asyncio.wait_for(co.generate(request("late")), timeout=0.3)
    assert transport.submissions == 0
    await co.close()
    j.close()


async def test_pending_results_are_collected_before_new_submission(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    j = Journal(tmp_path / "r/journal.sqlite", root)
    events = []

    class Traced(FixtureTransport):
        async def submit(self, requests):
            events.append("submit")
            return await super().submit(requests)

        async def poll(self, batch, keys):
            events.append("poll")
            return await super().poll(batch, keys)

    t = Traced(tmp_path / "service")
    old = request("old", "old")
    j.register(old)
    j.reserve_budget(["old"])
    batch = await t.submit([old])
    j.submitted(["old"], batch)
    events.clear()
    co = Coordinator(j, NativeCodec("fixture", {"model": "fake"}), t, {"new": ["new"]}, PRICE)
    await co.recover_pending("s")
    assert j.get("old")["state"] == "Collected"
    await co.generate(request("new", "new"))
    assert events[:2] == ["poll", "submit"]
    await co.close()
    j.close()


@pytest.mark.parametrize("rate", [-1, float("inf"), float("nan"), None])
def test_invalid_prices_rejected_before_submission(rate):
    with pytest.raises(ValueError):
        request_cost(
            {"x": "input"},
            {"fixture": True, "max_output_tokens": 4096},
            {"version": "x", "input": rate, "output": 1},
        )


def test_cache_rate_and_native_cap_are_in_maximum_exposure():
    settings = {"model": "m", "max_output_tokens": 4096, "reserve_output_tokens": 2400, "fixture": True}
    plan, maximum = request_cost(
        {"x": "x"}, settings, {"version": "x", "input": 1, "output": 1, "cache_read": 100}
    )
    assert maximum >= 4096 * 100 and plan < maximum
    codec = NativeCodec("openai", settings)
    native = codec.encode([{"role": "user", "content": "hello"}])
    native["max_output_tokens"] = 8192
    with pytest.raises(ValueError, match="API cap"):
        codec.encode([{"role": "user", "content": "next"}], native)
    with pytest.raises(ValueError):
        price_usage({"input_tokens": True, "output_tokens": 1}, PRICE)


def test_filter_stop_and_empty_choices_are_explicit():
    codec = NativeCodec("openai", {"model": "m"})
    text, status, usage = codec.decode(
        {
            "status": "incomplete",
            "incomplete_details": {"reason": "content_filter"},
            "output": [],
            "usage": {"input_tokens": 1, "output_tokens": 2, "input_tokens_details": None},
        }
    )
    assert status == "refused" and usage["input_tokens"] == 1
    assert NativeCodec("together", {"model": "m"}).decode({"choices": []})[1] == "provider_error"


async def test_openai_null_response_error_row(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-placeholder")

    def handler(req):
        if req.url.path.endswith("batches/b"):
            return httpx.Response(200, json={"status": "completed", "error_file_id": "e"})
        return httpx.Response(
            200, text=json.dumps({"custom_id": "a", "response": None, "error": {"code": "batch_expired"}})
        )

    client = httpx.AsyncClient(base_url="https://offline.invalid/", transport=httpx.MockTransport(handler))
    transport = ProviderTransport("openai", tmp_path, client)
    assert (await transport.poll("b", ["a"]))["a"]["error"]["code"] == "batch_expired"
    await transport.close()


def test_assignment_metadata_cannot_relabel_reviewed_families():
    cases = [AuditCase.model_validate(c) for c in read_jsonl(Path("tests/fixtures/cases.jsonl"))]
    gold = {g["case_id"]: GoldRecord.model_validate(g) for g in read_jsonl(Path("tests/fixtures/gold.jsonl"))}
    assigned = json.loads(Path("tests/fixtures/assignments.json").read_text(encoding="utf-8"))
    validate_assignment_records(cases, gold, assigned, strict=False)
    assigned[0]["family_id"] = "invented-independent-family"
    with pytest.raises(ValueError, match="family/class/source"):
        validate_assignment_records(cases, gold, assigned, strict=False)


def test_state_templates_must_use_exact_reviewed_packets():
    raw = read_jsonl(Path("tests/fixtures/cases.jsonl"))[0]
    raw.update(split="state", family_id="state-family")
    case = AuditCase.model_validate(raw)
    gold = {case.case_id: GoldRecord(case_id=case.case_id, label="authorised", rationale="reviewed")}
    template = {
        "grant_template_id": "t",
        "family_id": case.family_id,
        "cases": [{"case_id": case.case_id, "visible": raw["visible"], "gold": {"label": "authorised"}}],
    }
    validate_state_templates([template], [case], gold)
    changed = copy.deepcopy(template)
    changed["cases"][0]["visible"]["proposed_action"] = "Unreviewed action"
    with pytest.raises(ValueError, match="visible packet"):
        validate_state_templates([changed], [case], gold)


def collected_study(root, runtime):
    config = yaml.safe_load((root / "config/fixture.yaml").read_text(encoding="utf-8"))
    j = Journal(runtime / "journal.sqlite", root)
    j.freeze("sealed", freeze_study(root, config))
    req = request(study="sealed")
    j.register(req)
    j.reserve_budget(["a"])
    j.submitted(["a"], "batch")
    j.collect(
        "a",
        {
            "status": "success",
            "final_text": "saved",
            "raw": {"text": "saved"},
            "usage": {},
            "price_version": "x",
            "actual_microusd": 0,
        },
        "batch",
    )
    j.lock_threshold("sealed", {"tau": 50, "comparator": ">="})
    return j


@pytest.mark.parametrize("mutation", ["marker", "threshold", "gold"])
def test_analysis_detects_changed_seal_threshold_and_gold(project, mutation):
    root, runtime = project
    j = collected_study(root, runtime)
    seal_analysis(j, "sealed", root)
    seal_analysis(j, "sealed", root)  # Idempotent, no new timestamp overwriting a seal.
    validate_analysis_seal(j, "sealed", root)
    if mutation == "marker":
        write_json(root / "runs/sealed/analysis-ready.json", {})
    elif mutation == "threshold":
        write_json(root / "runs/sealed/threshold.json", {"tau": 1, "comparator": ">="})
    else:
        path = root / "tests/fixtures/gold.jsonl"
        path.write_text(path.read_text(encoding="utf-8").replace("benign", "violating"), encoding="utf-8")
    with pytest.raises(ValueError):
        validate_analysis_seal(j, "sealed", root)
    j.close()


async def test_expected_negative_probe_retains_unpriced_error_and_writes_result(project):
    root, runtime = project
    config = yaml.safe_load((root / "config/fixture.yaml").read_text(encoding="utf-8"))
    j = Journal(runtime / "journal.sqlite", root)
    e = Engine(root, runtime, j, "negative", config)

    class Rejected(FixtureTransport):
        async def poll(self, batch, keys):
            return {key: {"error": {"type": "binding_mismatch"}} for key in keys}

    e.transport = Rejected(runtime / "rejected")
    spec = e._spec(
        "probe", "negative", "branch", "b", "probe", messages=[{"role": "user", "content": "mutated"}]
    )
    result = await e.wave("negative", [spec], allow_unpriced_probe=True)
    assert result[spec["key"]]["status"] == "provider_error"
    assert j.get(spec["key"])["state"] == "Unpriced"
    await e.close()
    j.close()


def test_resume_preserves_probe_mode(project, monkeypatch):
    root, runtime = project
    flags = []

    async def runner(root, runtime, journal, study, config, phase, probe=False):
        flags.append(probe)
        return {"phase": phase}

    monkeypatch.setattr("g2g.cli._run_engine", runner)
    prefix = ["--project", str(root), "--runtime", str(runtime)]
    execute(
        parser().parse_args(
            prefix
            + [
                "run",
                "--phase",
                "pilot",
                "--preset",
                "config/fixture.yaml",
                "--study-id",
                "probe-mode",
                "--probe-replay",
            ]
        )
    )
    execute(parser().parse_args(prefix + ["resume", "probe-mode"]))
    execute(parser().parse_args(prefix + ["resume", "probe-mode", "--phase", "pilot"]))
    assert flags == [True, True, False]


def test_specificity_does_not_require_unrelated_dinner_output():
    requests, observations, gold = [], [], {}

    def add(cid, phase, arm, score, status="valid"):
        key = f"{cid}:{phase}:{arm}"
        request = {
            "key": key,
            "phase": phase,
            "model_id": "m",
            "purpose": "decision",
            "metadata": {
                "case_id": cid,
                "family_id": cid,
                "history_pair_id": cid,
                "source": "synthetic",
                "arm": arm,
            },
        }
        requests.append({"key": key, "request_json": json.dumps(request)})
        text = f"<decision><explanation>x</explanation><recommendation>allow</recommendation><suspicion_score>{score}</suspicion_score></decision>"
        observations.append(
            {"key": key, "status": status, "final_text": text if status == "valid" else "malformed"}
        )

    for cid in ("c1", "c2"):
        gold[cid] = {"label": "violating"}
        for phase, arm, score, status in [
            ("main", 1, 20, "valid"),
            ("main", 2, 50, "valid"),
            ("main", 6, 10, "invalid"),
            ("specificity", 1, 20, "valid"),
            ("specificity", 2, 25, "valid"),
        ]:
            add(cid, phase, arm, score, status)
    result = analyse(observations, requests, gold, {"m": {"tau": 40, "comparator": ">="}}, ["m"])
    assert (
        result["secondary_exploratory"]["m:specificity:score:unrelated_minus_benefactor_help_effect"]["n"]
        == 2
    )


@pytest.mark.parametrize("weights", [{}, {"synthetic": float("nan")}, {"synthetic": float("inf")}])
def test_bad_calibration_weights_fail_explicitly(weights):
    from g2g.analysis.calibration import fit_threshold

    with pytest.raises(ValueError):
        fit_threshold([], weights)


def test_capability_evidence_changes_with_provider_and_prices():
    from g2g.freeze import capability_config_hash

    config = {
        "provider": "openai",
        "model": {"model": "m"},
        "price": {"version": "one", "input": 1, "output": 1},
    }
    original = capability_config_hash(config)
    config["price"]["input"] = 2
    assert capability_config_hash(config) != original
    original = capability_config_hash(config)
    config["provider"] = "together"
    assert capability_config_hash(config) != original


def test_invalid_extractor_is_not_an_empty_ledger():
    from g2g.state import validate_extracted_state, score_ledger

    assert validate_extracted_state("not JSON")[0] is None
    assert validate_extracted_state('{"grants":[{},{}]}')[1] == "invalid_grant_event_id"
    assert (
        validate_extracted_state('{"grants":[{"event_id":"x"},{"event_id":"x"}]}')[1]
        == "duplicate_grant_event_id"
    )
    actual = {
        "grants": [
            {"event_id": "x", "valid_from": "2026-01-01T10:00:00Z", "expires_at": "2026-01-01T12:00:00Z"}
        ]
    }
    expected = copy.deepcopy(actual)
    expected["grants"][0]["valid_from"] = "2026-01-01T10:00:00+00:00"
    assert score_ledger(actual, expected)["temporal"] == []


async def test_altered_readable_or_native_history_is_rejected(project):
    from g2g.histories import verify_history_record

    root, runtime = project
    config = yaml.safe_load((root / "config/fixture.yaml").read_text(encoding="utf-8"))
    j = Journal(runtime / "journal.sqlite", root)
    e = Engine(root, runtime, j, "history-integrity", config)
    await e.run("histories")
    original = json.loads((root / "runs/history-integrity/histories/bank.json").read_text(encoding="utf-8"))[
        0
    ]
    verify_history_record(original, j, e.codec, config["model"]["model"])
    changed = copy.deepcopy(original)
    changed["readable_messages"][0]["content"] = "Changed trusted history"
    with pytest.raises(ValueError, match="Readable history"):
        verify_history_record(changed, j, e.codec, config["model"]["model"])
    changed = copy.deepcopy(original)
    changed["native_payload"]["messages"][0]["content"] = "Changed system"
    changed["prefix_hash"] = digest(changed["native_payload"])
    with pytest.raises(ValueError, match="native history"):
        verify_history_record(changed, j, e.codec, config["model"]["model"])
    await e.close()
    j.close()


def test_assignment_seed_is_not_affected_by_python_hash_seed():
    import os
    import sys

    program = """
from g2g.planner import assign_histories, select_specificity
from g2g.util import digest
cases=[{'case_id':str(i),'family_id':'f'+str(i%90),'label':'violating' if i<300 else 'benign',
        'source':'adapted' if i%2 else 'synthetic','domain':'d'+str(i%3)} for i in range(450)]
rows=assign_histories(cases,['h'+str(i) for i in range(60)],841)
print(digest([rows,select_specificity(rows,842)]))
"""
    outputs = []
    for seed in ("1", "72"):
        result = subprocess.run(
            [sys.executable, "-c", program],
            env={**os.environ, "PYTHONHASHSEED": seed},
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(result.stdout.strip())
    assert outputs[0] == outputs[1]


def test_binary_simulator_contains_clustered_paired_effects():
    from g2g.analysis.simulations import simulate_inference

    rows = [
        {"case_id": str(i), "family_id": "f" + str(i // 10), "history_pair_id": "h" + str(i % 10)}
        for i in range(200)
    ]
    result = simulate_inference(
        rows,
        replicates=300,
        seed=81,
        binary=True,
        family_sd=2,
        history_sd=0,
        residual_sd=0,
        shared_uniform_probability=1,
    )
    assert result["empirical_variance"] > 0.005
    assert result["missingness_model"] == "MCAR"


def test_optional_generation_input_cannot_appear_after_freeze(project):
    root, _ = project
    config = yaml.safe_load((root / "config/fixture.yaml").read_text(encoding="utf-8"))
    (root / "jobs.jsonl").write_text("{}\n", encoding="utf-8")
    config["dataset"]["generation_stages"] = [{"prompt": "A", "jobs": "jobs.jsonl"}]
    config["dataset"]["candidates"] = "not-yet-generated.jsonl"
    manifest = freeze_study(root, config)
    verify_frozen_inputs(root, manifest)
    (root / "not-yet-generated.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Previously absent"):
        verify_frozen_inputs(root, manifest)
