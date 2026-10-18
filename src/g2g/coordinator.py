from __future__ import annotations

import asyncio
import json
import sqlite3

from g2g.analysis.parser import parse_decision, apply_threshold
from g2g.budget import price_usage
from g2g.journal import ReconciliationRequired
from g2g.models import Request


class BlockFailure(RuntimeError):
    pass


class Coordinator:
    def __init__(
        self,
        journal,
        codec,
        transport,
        expected: dict[str, list[str]],
        price: dict,
        watchdog=30,
        poll_interval=5,
        http_limit=2,
        study_limit=None,
    ):
        flat = [key for keys in expected.values() for key in keys]
        if any(not keys for keys in expected.values()) or len(set(flat)) != len(flat):
            raise ValueError("Frozen block membership must be nonempty and unique")
        self.journal, self.codec, self.transport = journal, codec, transport
        self.expected, self.price = expected, price
        self.watchdog, self.poll_interval = watchdog, poll_interval
        self.futures, self.tasks, self.batch_tasks = {}, {}, {}
        self.failed_keys = {}
        self.http = asyncio.Semaphore(http_limit)
        self.halted = False
        self.study_limit = study_limit
        self.study_id = None
        self.failure = None

    async def generate(self, request: Request):
        if request.block_id not in self.expected or request.key not in self.expected[request.block_id]:
            raise ValueError("Request absent from frozen block membership")
        if self.study_id is not None and self.study_id != request.study_id:
            raise ValueError("Coordinator cannot mix studies")
        self.study_id = request.study_id
        row = self.journal.register(request)
        if row["state"] == "Collected":
            return self.journal.replay(request.key), "journal_replay"
        if row["state"] in ("Unknown", "Reserved", "Unpriced"):
            raise ReconciliationRequired("Acceptance or usage unresolved; no replacement submitted")
        if request.key in self.failed_keys:
            raise self.failed_keys[request.key]
        if self.halted:
            raise BlockFailure("Submission paused")
        if request.key not in self.futures:
            self.futures[request.key] = asyncio.get_running_loop().create_future()
        if row["state"] == "Submitted":
            self._attach(row["batch_id"])
        elif request.block_id not in self.tasks:
            self.tasks[request.block_id] = asyncio.create_task(self._barrier(request.block_id))
        return await asyncio.shield(self.futures[request.key]), "provider"

    def _fail(self, keys, exc):
        for key in keys:
            self.failed_keys[key] = exc
            future = self.futures.get(key)
            if future is not None and not future.done():
                future.set_exception(exc)

    def abort(self, exc):
        self.halted = True
        self.failure = exc
        self._fail([k for keys in self.expected.values() for k in keys], exc)

    async def recover_pending(self, study_id):
        """Collect saved accepted IDs before admitting any new paid submission."""
        self.study_id = study_id
        rows = self.journal.requests(study_id)
        if any(r["state"] in ("Unknown", "Reserved", "Unpriced") for r in rows):
            raise ReconciliationRequired("Resolve acceptance/charges before resuming")
        batches = {r["batch_id"] for r in rows if r["state"] == "Submitted"}
        for batch in sorted(batches):
            self._attach(batch)
        if batches:
            await asyncio.gather(*(self.batch_tasks[b] for b in sorted(batches)))
        if self.failure:
            raise self.failure
        if any(
            r["state"] != "Collected" for r in self.journal.requests(study_id) if r["batch_id"] in batches
        ):
            raise ReconciliationRequired("Recovered batch usage/outcomes require reconciliation")

    async def _barrier(self, block_id):
        keys = self.expected[block_id]
        try:

            async def ready():
                while True:
                    rows = [self.journal.get(k) for k in keys]
                    if all(r is not None for r in rows):
                        return rows
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(ready(), timeout=self.watchdog)
            # Admission, reservation and the decision to POST are in the same
            # semaphore section. Queued chunks cannot escape a later fatal pause.
            async with self.http:
                if self.halted:
                    raise BlockFailure("Submission paused")
                rows = [self.journal.get(k) for k in keys]
                if any(r["state"] in ("Unknown", "Reserved", "Unpriced") for r in rows):
                    raise ReconciliationRequired("Block contains unresolved member")
                for row in rows:
                    if row["state"] == "Submitted":
                        self._attach(row["batch_id"])
                unsent = [r for r in rows if r["state"] == "Planned"]
                if not unsent:
                    return
                requests = [Request.model_validate_json(r["request_json"]) for r in unsent]
                self.journal.reserve_budget([r.key for r in requests], study_limit=self.study_limit)
                with self.journal.db:
                    self.journal.event(
                        requests[0].study_id,
                        None,
                        "submit_chunk",
                        "cost_bounds",
                        {
                            "planned_microusd": sum(r.planning_microusd for r in requests),
                            "maximum_microusd": sum(r.maximum_microusd for r in requests),
                            "membership": [r.key for r in requests],
                        },
                    )
                try:
                    batch_id = await self.transport.submit(requests)
                    # No await between accepted ID return and synchronous commit.
                    self.journal.submitted([r.key for r in requests], batch_id)
                except BaseException as exc:
                    self.journal.unknown([r.key for r in requests], repr(exc))
                    raise
                self.journal.snapshot_journal()
                self._attach(batch_id)
        except BaseException as exc:
            failure = BlockFailure(str(exc))
            # Resolve waiters even when the journal itself can no longer write.
            self.abort(failure)
            try:
                with self.journal.db:
                    self.journal.event(
                        self.study_id or "",
                        None,
                        "block_barrier",
                        "failed",
                        {"block": block_id, "reason": repr(exc)},
                    )
            except (OSError, sqlite3.Error):
                pass

    def _attach(self, batch_id):
        if batch_id not in self.batch_tasks:
            self.batch_tasks[batch_id] = asyncio.create_task(self._collect(batch_id))

    async def _collect(self, batch_id):
        rows = [
            dict(r)
            for r in self.journal.db.execute(
                "SELECT * FROM requests WHERE batch_id=? AND study_id=?", (batch_id, self.study_id)
            )
        ]
        keys = [r["key"] for r in rows]
        try:
            while True:
                async with self.http:
                    result = await self.transport.poll(batch_id, keys)
                if result is not None:
                    break
                await asyncio.sleep(self.poll_interval)
            for row in rows:
                if self.journal.get(row["key"])["state"] == "Collected":
                    observation = self.journal.replay(row["key"])
                else:
                    raw = result[row["key"]]
                    try:
                        text, status, usage = self.codec.decode(raw)
                        actual = price_usage(usage, self.price)
                    except (ValueError, TypeError, KeyError, IndexError) as exc:
                        text, status, usage, actual = "", "provider_error", {}, None
                        raw = {"error": {"decode_gate": str(exc)}, "original": raw}
                    req = json.loads(row["request_json"])
                    decision = parse_decision(text, status) if req["purpose"] == "decision" else None
                    observation = self.journal.collect(
                        row["key"],
                        {
                            "raw": raw,
                            "final_text": text,
                            "status": decision.status if decision else status,
                            "usage": usage,
                            "actual_microusd": actual,
                            "price_version": self.price["version"],
                        },
                        batch_id,
                    )
                    with self.journal.db:
                        self.journal.event(
                            row["study_id"],
                            row["key"],
                            "parse_decision" if decision else "decode",
                            observation["status"],
                            {"reason": decision.reason if decision else None},
                        )
                    threshold_path = self.journal.project / "runs" / row["study_id"] / "threshold.json"
                    if decision and threshold_path.exists():
                        threshold = self.journal.lock_threshold(row["study_id"], None)
                        applied = apply_threshold(decision, threshold)
                        with self.journal.db:
                            self.journal.event(
                                row["study_id"], row["key"], "apply_threshold", applied["operative"], applied
                            )
                    self.journal.technical_pause_check(req["model_id"], req["study_id"])
                future = self.futures.get(row["key"])
                if future is not None and not future.done():
                    future.set_result(observation)
            self.journal.snapshot_journal()
        except BaseException as exc:
            self.abort(exc)

    async def close(self):
        self.halted = True
        tasks = list(self.tasks.values()) + list(self.batch_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.journal.snapshot_journal()
