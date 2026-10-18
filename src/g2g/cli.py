from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import yaml

from g2g.freeze import freeze_study, readiness, verify_frozen_inputs
from g2g.journal import Journal, RunnerLock
from g2g.planner import PHASES, WORKLOAD
from g2g.util import ensure_local_runtime, runtime_dir, source_commit, utcnow, write_json


def parser():
    p = argparse.ArgumentParser(prog="g2g", description="Local journal-backed text-audit research harness")
    p.add_argument("--project", type=Path, default=Path.cwd())
    p.add_argument("--runtime", type=Path, default=None)
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--phase", choices=PHASES, required=True)
    run.add_argument("--preset", default="fixture")
    run.add_argument("--study-id")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--probe-replay", action="store_true")
    resume = sub.add_parser("resume")
    resume.add_argument("study_id")
    resume.add_argument("--phase", choices=PHASES)
    report = sub.add_parser("report")
    report.add_argument("study_id")
    report.add_argument("--analysis", action="store_true")
    report.add_argument("--debug-key")
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("key", nargs="+")
    reconcile.add_argument("--batch-id")
    reconcile.add_argument("--definitely-unsent", action="store_true")
    reconcile.add_argument("--evidence", required=True)
    usage = sub.add_parser("reconcile-usage")
    usage.add_argument("key")
    usage.add_argument("actual_microusd", type=int)
    usage.add_argument("--evidence", required=True)
    charge = sub.add_parser("charge")
    charge.add_argument("charge_id")
    charge.add_argument("amount_microusd", type=int)
    charge.add_argument("--description", required=True)
    assign = sub.add_parser("assign")
    assign.add_argument("--cases", type=Path, required=True)
    assign.add_argument("--gold", type=Path, required=True)
    assign.add_argument("--scripts", type=Path, required=True)
    assign.add_argument("--seed", type=int, required=True)
    assign.add_argument("--output", type=Path, required=True)
    simulate = sub.add_parser("simulate")
    simulate.add_argument("--assignments", type=Path, required=True)
    simulate.add_argument("--replicates", type=int, default=1000)
    simulate.add_argument("--seed", type=int, default=20261018)
    simulate.add_argument("--binary", action="store_true")
    simulate.add_argument("--missing-rate", type=float, default=0)
    simulate.add_argument("--output", type=Path, required=True)
    seal = sub.add_parser("seal-analysis")
    seal.add_argument("study_id")
    panel = sub.add_parser("panel-report")
    panel.add_argument("studies", nargs=4)
    panel.add_argument("--output", type=Path, required=True)
    export = sub.add_parser("export")
    export.add_argument("log", type=Path)
    export.add_argument("output", type=Path)
    return p


def validate_id(value):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}", value):
        raise ValueError("Study ID must use letters, digits, underscore or hyphen")
    return value


async def _run_engine(root, runtime, journal, study, config, phase, probe=False):
    from g2g.engine import Engine

    engine = Engine(root, runtime, journal, study, config)
    try:
        if probe:
            from g2g.probes import probe_replay

            return await probe_replay(engine)
        return await engine.run(phase)
    finally:
        await engine.close()


def execute(args):
    root = args.project.expanduser().resolve()
    runtime = (args.runtime or runtime_dir()).expanduser().resolve()
    ensure_local_runtime(runtime, root)
    if args.command == "assign":
        from g2g.operations import prepare_assignments

        return prepare_assignments(args.cases, args.gold, args.scripts, args.seed, args.output)
    if args.command == "simulate":
        from g2g.analysis.simulations import simulate_inference

        result = simulate_inference(
            json.loads(args.assignments.read_text(encoding="utf-8")),
            args.replicates,
            args.seed,
            args.binary,
            missing_rate=args.missing_rate,
        )
        write_json(args.output, result)
        return result
    if args.command == "export":
        from g2g.reporting import export_control_arena

        return {"samples": export_control_arena(args.log, args.output)}
    if args.command == "report":
        from g2g.reporting import report

        journal = Journal(runtime / "journal.sqlite", root, readonly=True)
        try:
            return report(journal, validate_id(args.study_id), root, args.analysis, args.debug_key)
        finally:
            journal.close()
    with RunnerLock(runtime):
        journal = Journal(runtime / "journal.sqlite", root, source_commit(root))
        try:
            if args.command == "seal-analysis":
                from g2g.operations import seal_analysis

                return seal_analysis(journal, validate_id(args.study_id), root)
            if args.command == "panel-report":
                from g2g.operations import panel_report

                return panel_report(journal, [validate_id(s) for s in args.studies], root, args.output)
            if args.command == "reconcile-usage":
                journal.reconcile_usage(args.key, args.actual_microusd, args.evidence)
                journal.snapshot_journal()
                return {"status": "usage_reconciled", "key": args.key}
            if args.command == "charge":
                journal.add_charge(args.charge_id, args.amount_microusd, args.description)
                journal.snapshot_journal()
                return journal.spending()
            if args.command == "reconcile":
                journal.reconcile(args.key, args.batch_id, args.evidence, args.definitely_unsent)
                journal.snapshot_journal()
                return {"status": "reconciled", "keys": args.key}
            if args.command == "run":
                config_path = Path(args.preset)
                if not config_path.is_file():
                    candidate = root / config_path
                    config_path = (
                        candidate if candidate.is_file() else root / "config" / (args.preset + ".yaml")
                    )
                config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                study = validate_id(
                    args.study_id
                    or "study-" + utcnow().replace(":", "").replace(".", "").replace("+", "")[:22]
                )
                phase = args.phase
                if args.dry_run:
                    # Dry-run does not initialise provider clients or call APIs.
                    return {
                        "dry_run": True,
                        "phase": phase,
                        "provider": config["provider"],
                        "planned_workload_per_model": WORKLOAD,
                        "planned_total": sum(WORKLOAD.values()) * 4,
                        "stop_usd": 450,
                        "hard_ceiling_usd": 500,
                        "manifest": freeze_study(root, config),
                        "note": "Live gates, actual token/cost bounds and frozen membership validated before submission",
                    }
                readiness(root, config, phase)
                manifest = freeze_study(root, config)
                journal.freeze(study, manifest)
                write_json(
                    root / "runs" / study / "manifest" / "last-phase.json",
                    {"phase": phase, "probe_replay": args.probe_replay},
                )
                return {
                    "study_id": study,
                    **asyncio.run(
                        _run_engine(root, runtime, journal, study, config, phase, args.probe_replay)
                    ),
                }
            study = validate_id(args.study_id)
            row = journal.db.execute(
                "SELECT manifest_json FROM studies WHERE study_id=?", (study,)
            ).fetchone()
            if not row:
                raise ValueError("Unknown study ID")
            manifest = json.loads(row[0])
            verify_frozen_inputs(root, manifest)
            # Verify restored-snapshot dependencies and raw objects before permitting paid work.
            for observation in journal.observations(study):
                journal.verify_archive(observation)
            unknown = [r["key"] for r in journal.requests(study) if r["state"] in ("Unknown", "Unpriced")]
            if unknown:
                raise ValueError("Reconcile acceptance/charges first: " + ",".join(unknown))
            last = json.loads(
                (root / "runs" / study / "manifest" / "last-phase.json").read_text(encoding="utf-8")
            )
            phase = args.phase or last["phase"]
            probe = bool(last.get("probe_replay")) and args.phase is None
            return {
                "study_id": study,
                **asyncio.run(_run_engine(root, runtime, journal, study, manifest["config"], phase, probe)),
            }
        finally:
            journal.snapshot_journal()
            journal.close()


def main():
    args = parser().parse_args()
    try:
        print(json.dumps(execute(args), indent=2, default=str))
    except KeyboardInterrupt:
        print("Paused. Accepted batches retain their IDs; use g2g report and g2g resume.", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print(f"g2g: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
