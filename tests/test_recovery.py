import asyncio
import sqlite3

import pytest

from g2g.adapters.codecs import NativeCodec
from g2g.adapters.transport import FixtureTransport
from g2g.coordinator import BlockFailure, Coordinator
from g2g.journal import BudgetExceeded, Journal, ReconciliationRequired, RunnerLock
from g2g.models import Request

PRICE = {"input": 0, "output": 0, "version": "fixture"}


def req(key="a", study="s", plan=1, maximum=2):
    return Request(
        key=key,
        study_id=study,
        phase="pilot",
        model_id="fake",
        purpose="decision",
        block_id="b",
        payload={"model": "fake", "messages": [{"role": "user", "content": key}]},
        config_hash="frozen",
        planning_microusd=plan,
        maximum_microusd=maximum,
    )


def setup(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    runtime = tmp_path / "runtime"
    journal = Journal(runtime / "journal.sqlite", project)
    return journal, FixtureTransport(runtime / "service"), NativeCodec("fixture", {"model": "fake"})


async def test_barrier_and_duplicate_replay(tmp_path):
    j, t, c = setup(tmp_path)
    coordinator = Coordinator(j, c, t, {"b": ["a", "b"]}, PRICE, watchdog=0.2)
    a, b = await asyncio.gather(coordinator.generate(req()), coordinator.generate(req("b")))
    assert t.submissions == 1
    assert a[0]["observation_id"] != b[0]["observation_id"]
    again, origin = await coordinator.generate(req())
    assert origin == "journal_replay" and again == a[0] and t.submissions == 1
    assert j.spending()["actual_microusd"] == 0
    await coordinator.close()
    j.close()


async def test_restart_submitted_and_collected(tmp_path):
    j, t, c = setup(tmp_path)
    j.register(req())
    j.reserve_budget(["a"])
    batch = await t.submit([req()])
    j.submitted(["a"], batch)
    j.snapshot_journal()
    j.close()
    j = Journal(tmp_path / "runtime/journal.sqlite", tmp_path / "project")
    t2 = FixtureTransport(tmp_path / "runtime/service")
    coordinator = Coordinator(j, c, t2, {"b": ["a"]}, PRICE)
    o, _ = await coordinator.generate(req())
    assert t2.submissions == 0 and o["batch_id"] == batch
    await coordinator.close()
    j.close()
    j = Journal(tmp_path / "runtime/journal.sqlite", tmp_path / "project")
    coordinator = Coordinator(j, c, t2, {"b": ["a"]}, PRICE)
    _, origin = await coordinator.generate(req())
    assert origin == "journal_replay" and t2.submissions == 0
    await coordinator.close()
    j.close()


def test_ambiguous_acceptance_holds_reservation(tmp_path):
    j, t, c = setup(tmp_path)
    j.register(req())
    j.reserve_budget(["a"])
    j.close()
    j = Journal(tmp_path / "runtime/journal.sqlite", tmp_path / "project")
    assert j.get("a")["state"] == "Unknown"
    assert j.spending()["reserved_microusd"] == 1
    j.register(req("b"))
    with pytest.raises(ReconciliationRequired):
        j.reserve_budget(["b"])
    with pytest.raises(ReconciliationRequired):
        j.reconcile(["a"], None, "No ID is not enough")
    j.reconcile(["a"], None, "Provider listing and charge audit verified not accepted", True)
    j.reserve_budget(["a"])
    j.close()


async def test_missing_block_member_terminates(tmp_path):
    j, t, c = setup(tmp_path)
    coordinator = Coordinator(j, c, t, {"b": ["a", "never"]}, PRICE, watchdog=0.03)
    with pytest.raises(BlockFailure):
        await asyncio.wait_for(coordinator.generate(req()), timeout=0.5)
    assert t.submissions == 0
    await coordinator.close()
    j.close()


def test_caps_project_wide_and_payload_conflict(tmp_path):
    j, t, c = setup(tmp_path)
    j.add_charge("prior", 449_000_000, "previous study")
    j.register(req(plan=2_000_000, maximum=3_000_000))
    with pytest.raises(BudgetExceeded):
        j.reserve_budget(["a"])
    j.register(req("b", "other", plan=1, maximum=52_000_000))
    with pytest.raises(BudgetExceeded):
        j.reserve_budget(["b"])
    assert j.get("a")["state"] == "Planned"
    with pytest.raises(ValueError):
        j.register(req(plan=2, maximum=3))
    j.close()


async def test_snapshot_failure_keeps_id_and_blocks_submission(tmp_path, monkeypatch):
    j, t, c = setup(tmp_path)
    original = j.snapshot_journal

    def fail():
        raise OSError("disk full")

    monkeypatch.setattr(j, "snapshot_journal", fail)
    coordinator = Coordinator(j, c, t, {"b": ["a"]}, PRICE)
    with pytest.raises(BlockFailure):
        await coordinator.generate(req())
    assert j.get("a")["state"] == "Submitted" and j.get("a")["batch_id"]
    assert t.submissions == 1 and coordinator.halted
    monkeypatch.setattr(j, "snapshot_journal", original)
    await coordinator.close()
    j.close()


def test_atomic_snapshot_and_second_runner(tmp_path):
    j, t, c = setup(tmp_path)
    j.register(req())
    path = j.snapshot_journal()
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert db.execute("SELECT count(*) FROM requests").fetchone()[0] == 1
    db.close()
    with RunnerLock(tmp_path / "lock"), pytest.raises(RuntimeError), RunnerLock(tmp_path / "lock"):
        pass
    j.close()


async def test_archive_corruption_is_not_replayed(tmp_path):
    j, t, c = setup(tmp_path)
    coordinator = Coordinator(j, c, t, {"b": ["a"]}, PRICE)
    o, _ = await coordinator.generate(req())
    (j.project / o["archive"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        j.replay("a")
    await coordinator.close()
    j.close()


async def test_failed_member_unpriced_usage_reconciliation(tmp_path):
    j, t, c = setup(tmp_path)

    class PartialTransport(FixtureTransport):
        async def poll(self, batch_id, keys):
            return {k: {"error": {"type": "expired"}} for k in keys}

    transport = PartialTransport(tmp_path / "runtime/service2")
    coordinator = Coordinator(j, c, transport, {"b": ["a"]}, PRICE)
    observation, _ = await coordinator.generate(req())
    assert observation["status"] == "provider_error" and j.get("a")["state"] == "Unpriced"
    j.register(req("b"))
    with pytest.raises(ReconciliationRequired):
        j.reserve_budget(["b"])
    j.reconcile_usage("a", 0, "Provider billing record confirms no charge for expired item")
    assert (
        j.get("a")["state"] == "Collected"
        and j.replay("a")["observation_id"] == observation["observation_id"]
    )
    assert j.spending()["actual_microusd"] == 0
    await coordinator.close()
    j.close()
