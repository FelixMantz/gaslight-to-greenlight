from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from g2g.models import Request
from g2g.util import atomic_write, canonical, digest, fsync_dir, utcnow


class BudgetExceeded(RuntimeError):
    pass


class ReconciliationRequired(RuntimeError):
    pass


class RunnerLock:
    def __init__(self, runtime: Path):
        runtime.mkdir(parents=True, exist_ok=True)
        self.path = runtime / "runner.lock"
        self.file = None

    def __enter__(self):
        self.file = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.file.seek(0)
                if not self.file.read(1):
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError("Another runner owns this project runtime") from exc
        return self

    def __exit__(self, *args):
        if self.file:
            if os.name == "nt":
                import msvcrt

                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()


class Journal:
    def __init__(self, path: Path, project: Path, source_commit: str = "fixture", readonly: bool = False):
        self.path, self.project, self.source_commit = path, project, source_commit
        if readonly:
            self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        if not readonly:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA foreign_keys=ON")
            if self.db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                raise RuntimeError("WAL unavailable")
            if self.db.execute("PRAGMA synchronous").fetchone()[0] != 2:
                raise RuntimeError("FULL synchronous unavailable")
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS requests (
              key TEXT PRIMARY KEY, study_id TEXT NOT NULL, block_id TEXT NOT NULL,
              payload_hash TEXT NOT NULL, request_json TEXT NOT NULL, state TEXT NOT NULL,
              batch_id TEXT, observation_json TEXT, plan INTEGER NOT NULL, maximum INTEGER NOT NULL,
              actual INTEGER, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY, study_id TEXT, key TEXT, attempt INTEGER, time TEXT,
              function TEXT, source_commit TEXT, branch TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS charges (
              charge_id TEXT PRIMARY KEY, amount INTEGER NOT NULL, description TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS studies (
              study_id TEXT PRIMARY KEY, manifest_hash TEXT NOT NULL, manifest_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS controls (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            # A crash between intent and persisted acceptance is ambiguous, not unsent.
            with self.db:
                self.db.execute("UPDATE requests SET state='Unknown' WHERE state='Reserved'")

    def close(self):
        self.db.close()

    def event(self, study, key, function, branch, detail=None, attempt=1):
        self.db.execute(
            "INSERT INTO events(study_id,key,attempt,time,function,source_commit,branch,detail) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (study, key, attempt, utcnow(), function, self.source_commit, branch, json.dumps(detail or {})),
        )

    def freeze(self, study: str, manifest: dict):
        value = canonical(manifest).decode()
        with self.db:
            old = self.db.execute("SELECT manifest_hash FROM studies WHERE study_id=?", (study,)).fetchone()
            if old and old[0] != digest(manifest):
                raise ValueError("Study ID already frozen with different inputs")
            self.db.execute("INSERT OR IGNORE INTO studies VALUES(?,?,?)", (study, digest(manifest), value))
        from g2g.util import write_json

        write_json(self.project / "runs" / study / "manifest" / "study.json", manifest, immutable=True)

    def get(self, key):
        row = self.db.execute("SELECT * FROM requests WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def register(self, request: Request):
        value = request.model_dump(mode="json")
        with self.db:
            old = self.get(request.key)
            if old:
                if old["payload_hash"] != digest(value):
                    raise ValueError("Request key reused with different bytes/configuration")
                return old
            self.db.execute(
                "INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    request.key,
                    request.study_id,
                    request.block_id,
                    digest(value),
                    canonical(value).decode(),
                    "Planned",
                    None,
                    None,
                    request.planning_microusd,
                    request.maximum_microusd,
                    None,
                    utcnow(),
                ),
            )
            self.event(request.study_id, request.key, "register", "planned", attempt=request.attempt)
        return self.get(request.key)

    def spending(self):
        row = self.db.execute("""SELECT coalesce(sum(actual),0) AS actual,
          coalesce(sum(CASE WHEN state IN ('Reserved','Unknown','Submitted','Unpriced') THEN plan ELSE 0 END),0) AS reserved,
          coalesce(sum(CASE WHEN state IN ('Reserved','Unknown','Submitted','Unpriced') THEN maximum ELSE 0 END),0) AS maximum
          FROM requests""").fetchone()
        extra = self.db.execute("SELECT coalesce(sum(amount),0) FROM charges").fetchone()[0]
        return {
            "actual_microusd": row["actual"] + extra,
            "reserved_microusd": row["reserved"],
            "in_flight_maximum_microusd": row["maximum"],
        }

    def add_charge(self, charge_id, amount, description):
        if amount < 0:
            raise ValueError("Negative charge")
        with self.db:
            old = self.db.execute("SELECT * FROM charges WHERE charge_id=?", (charge_id,)).fetchone()
            if old and (old["amount"] != amount or old["description"] != description):
                raise ValueError("Charge ID conflict")
            self.db.execute("INSERT OR IGNORE INTO charges VALUES(?,?,?)", (charge_id, amount, description))

    def reserve_budget(self, keys, operational=450_000_000, hard=500_000_000, study_limit=None):
        if not keys or len(set(keys)) != len(keys):
            raise ValueError("Empty/duplicate submission")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute("SELECT 1 FROM controls WHERE key='exposure_violation'").fetchone():
                raise ReconciliationRequired(
                    "Observed charge exceeded verified maximum; configuration review required"
                )
            if self.db.execute(
                "SELECT 1 FROM requests WHERE state IN ('Unknown','Unpriced') LIMIT 1"
            ).fetchone():
                raise ReconciliationRequired("Unknown acceptance / unreconciled usage blocks new submissions")
            rows = [self.get(key) for key in keys]
            if any(r is None or r["state"] != "Planned" for r in rows):
                raise ValueError("Only known unsent requests can be reserved")
            if study_limit is not None:
                studies = {r["study_id"] for r in rows}
                if len(studies) != 1:
                    raise ValueError("Chunk cannot mix study spending caps")
                study = next(iter(studies))
                exposure = self.db.execute(
                    "SELECT coalesce(sum(CASE WHEN actual IS NOT NULL THEN actual WHEN state IN ('Reserved','Unknown','Submitted','Unpriced') THEN maximum ELSE 0 END),0) FROM requests WHERE study_id=?",
                    (study,),
                ).fetchone()[0]
                if exposure + sum(r["maximum"] for r in rows) > study_limit:
                    raise BudgetExceeded("Small preset study exposure cap")
            spend = self.spending()
            if (
                spend["actual_microusd"] + spend["reserved_microusd"] + sum(r["plan"] for r in rows)
                > operational
            ):
                raise BudgetExceeded("$450 operational stop")
            if (
                spend["actual_microusd"]
                + spend["in_flight_maximum_microusd"]
                + sum(r["maximum"] for r in rows)
                > hard
            ):
                raise BudgetExceeded("$500 absolute exposure guard")
            for row in rows:
                req = json.loads(row["request_json"])
                if self.db.execute(
                    "SELECT 1 FROM controls WHERE key=?", (f"paused:{req['study_id']}:{req['model_id']}",)
                ).fetchone():
                    raise ReconciliationRequired(
                        "Model technical-invalid-output pause; frozen review required"
                    )
                for dependency in req["dependencies"]:
                    dep = self.get(dependency)
                    if not dep or dep["state"] != "Collected":
                        raise ValueError("Dependency not durably collected")
                self.db.execute(
                    "UPDATE requests SET state='Reserved',updated_at=? WHERE key=?", (utcnow(), row["key"])
                )
                self.event(row["study_id"], row["key"], "reserve_budget", "intent_committed")
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def submitted(self, keys, batch_id):
        if not batch_id:
            raise ValueError("Empty provider ID")
        with self.db:
            for key in keys:
                row = self.get(key)
                if row["state"] not in ("Reserved", "Unknown"):
                    raise ValueError("Illegal submission transition")
                self.db.execute(
                    "UPDATE requests SET state='Submitted',batch_id=?,updated_at=? WHERE key=?",
                    (batch_id, utcnow(), key),
                )
                self.event(
                    row["study_id"], key, "submit_chunk", "accepted_id_committed", {"batch_id": batch_id}
                )

    def unknown(self, keys, reason):
        with self.db:
            for key in keys:
                row = self.get(key)
                if row["state"] == "Reserved":
                    self.db.execute("UPDATE requests SET state='Unknown' WHERE key=?", (key,))
                    self.event(
                        row["study_id"], key, "submit_chunk", "unknown_acceptance", {"reason": str(reason)}
                    )

    def reconcile(self, keys, batch_id: str | None, evidence: str, definitely_unsent=False):
        if not evidence.strip():
            raise ValueError("Reconciliation evidence required")
        if batch_id:
            self.submitted(keys, batch_id)
        elif definitely_unsent:
            with self.db:
                for key in keys:
                    if self.get(key)["state"] != "Unknown":
                        raise ValueError("Only unknown attempts may be reconciled")
                    self.db.execute("UPDATE requests SET state='Planned' WHERE key=?", (key,))
                    self.event(
                        self.get(key)["study_id"], key, "reconcile", "verified_unsent", {"evidence": evidence}
                    )
        else:
            raise ReconciliationRequired("Missing ID is not evidence of non-acceptance")
        with self.db:
            self.event(
                self.get(keys[0])["study_id"], None, "reconcile", "operator_evidence", {"evidence": evidence}
            )

    def reconcile_usage(self, key: str, actual_microusd: int, evidence: str):
        row = self.get(key)
        if not row or row["state"] != "Unpriced" or actual_microusd < 0 or not evidence.strip():
            raise ValueError("Unpriced observation, nonnegative actual charge and evidence required")
        with self.db:
            self.db.execute(
                "UPDATE requests SET actual=?,state='Collected',updated_at=? WHERE key=?",
                (actual_microusd, utcnow(), key),
            )
            self.event(
                row["study_id"],
                key,
                "reconcile_usage",
                "exact_charge",
                {"actual_microusd": actual_microusd, "evidence": evidence},
            )
            if actual_microusd > row["maximum"]:
                self.db.execute("INSERT OR REPLACE INTO controls VALUES('exposure_violation','true')")

    def technical_pause_check(self, model_id: str, study_id: str):
        arms = {}
        for row in self.db.execute(
            "SELECT request_json,observation_json FROM requests WHERE observation_json IS NOT NULL"
        ):
            req = json.loads(row["request_json"])
            if (
                req["study_id"] != study_id
                or req["model_id"] != model_id
                or req["purpose"] != "decision"
                or "arm" not in req["metadata"]
            ):
                continue
            arm = (req["phase"], req["metadata"]["arm"])
            counts = arms.setdefault(arm, [0, 0])
            counts[0] += 1
            counts[1] += json.loads(row["observation_json"])["status"] != "valid"
        if any(n >= 100 and bad / n > 0.02 for n, bad in arms.values()):
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO controls VALUES(?,?)",
                    (f"paused:{study_id}:{model_id}", "invalid_output_rate"),
                )
            return True
        return False

    def archive(self, study: str, value: dict) -> tuple[str, str]:
        data = gzip.compress(canonical(value) + b"\n", mtime=0)
        sha = hashlib.sha256(data).hexdigest()
        path = self.project / "runs" / "batches" / study / f"chunk_{sha}.jsonl.gz"
        atomic_write(path, data, immutable=True)
        return str(path.relative_to(self.project)), sha

    def collect(self, key, result: dict, batch_id: str):
        row = self.get(key)
        if row["state"] in ("Collected", "Unpriced"):
            observation = json.loads(row["observation_json"])
            self.verify_archive(observation)
            # A repeated collection must not silently replace an earlier result.
            if digest(observation["raw"]) != digest(result["raw"]):
                raise ValueError("Conflicting duplicate provider result")
            return observation
        if row["state"] != "Submitted" or row["batch_id"] != batch_id:
            raise ValueError("Collection does not match accepted provider ID")
        req = json.loads(row["request_json"])
        path, sha = self.archive(row["study_id"], {"request": req, "result": result, "batch_id": batch_id})
        observation = {
            **result,
            "key": key,
            "observation_id": digest({"key": key, "attempt": req["attempt"]}),
            "batch_id": batch_id,
            "collected_at": utcnow(),
            "archive": path,
            "archive_sha256": sha,
        }
        actual = result.get("actual_microusd")
        if actual is not None and actual < 0:
            raise ValueError("Negative billed usage")
        # An overrun is retained, then blocks further submissions. Never hide it.
        state = "Collected" if actual is not None else "Unpriced"
        with self.db:
            self.db.execute(
                "UPDATE requests SET state=?,observation_json=?,actual=?,updated_at=? WHERE key=?",
                (state, canonical(observation).decode(), actual, utcnow(), key),
            )
            self.event(
                row["study_id"],
                key,
                "collect_chunk",
                state,
                {"observation_id": observation["observation_id"]},
            )
            if actual is not None and actual > row["maximum"]:
                self.db.execute("INSERT OR REPLACE INTO controls VALUES('exposure_violation','true')")
        return observation

    def verify_archive(self, observation):
        path = (self.project / observation["archive"]).resolve()
        if (
            self.project.resolve() not in path.parents
            or hashlib.sha256(path.read_bytes()).hexdigest() != observation["archive_sha256"]
        ):
            raise ValueError("Missing or corrupt immutable raw archive")

    def replay(self, key):
        row = self.get(key)
        if row["state"] == "Unpriced":
            raise ReconciliationRequired("Provider usage requires reconciliation")
        if row["state"] != "Collected":
            raise ValueError("Observation unavailable")
        observation = json.loads(row["observation_json"])
        self.verify_archive(observation)
        with self.db:
            self.event(
                row["study_id"],
                key,
                "generate",
                "journal_replay",
                {
                    "observation_id": observation["observation_id"],
                    "original_time": observation["collected_at"],
                },
            )
        return observation

    def snapshot_journal(self):
        target = self.project / "runs" / "journal_snapshot.sqlite"
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".journal-", dir=target.parent)
        os.close(fd)
        try:
            destination = sqlite3.connect(name)
            try:
                self.db.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Snapshot integrity failure")
            finally:
                destination.close()
            with open(name, "rb") as f:
                os.fsync(f.fileno())
            os.replace(name, target)
            fsync_dir(target.parent)
        finally:
            Path(name).unlink(missing_ok=True)
        return target

    def observations(self, study):
        return [
            json.loads(r[0])
            for r in self.db.execute(
                "SELECT observation_json FROM requests WHERE study_id=? AND observation_json IS NOT NULL ORDER BY key",
                (study,),
            )
        ]

    def requests(self, study):
        return [
            dict(r) for r in self.db.execute("SELECT * FROM requests WHERE study_id=? ORDER BY key", (study,))
        ]
