from __future__ import annotations

import json
from datetime import datetime
from collections import defaultdict
from types import SimpleNamespace

from inspect_ai import eval_async

from g2g.adapters.codecs import NativeCodec
from g2g.adapters.transport import FixtureTransport, ProviderTransport
from g2g.analysis.calibration import fit_threshold
from g2g.compiler import SYSTEM, compile_identity_bindings
from g2g.coordinator import Coordinator
from g2g.freeze import readiness
from g2g.histories import audit_history_balance, validate_script, verify_history_record
from g2g.inspect_tasks import provider  # registers the one model-call path
from g2g.inspect_tasks.tasks import audit_task
from g2g.models import IdentityBinding
from g2g.planner import canonical_blocks, request_key, validate_assignments
from g2g.state import build_state_histories, extraction_prompt, validate_extracted_state
from g2g.util import atomic_write, canonical, digest, read_jsonl, write_json


def _load(root, config, name):
    path = root / config["inputs"][name]
    return (
        read_jsonl(path)
        if path.suffix in (".jsonl", ".txt")
        else json.loads(path.read_text(encoding="utf-8"))
    )


class Engine:
    def __init__(self, root, runtime, journal, study_id, config):
        self.root, self.runtime, self.journal, self.study_id, self.config = (
            root,
            runtime,
            journal,
            study_id,
            config,
        )
        self.settings, self.price = config["model"], config["price"]
        self.codec = NativeCodec(config["provider"], self.settings)
        self.stage = root / "runs" / study_id
        self.transport = (
            FixtureTransport(runtime / "fake-provider")
            if config["provider"] == "fixture"
            else ProviderTransport(config["provider"], self.stage / "native_sync")
        )
        self.coordinator = None

    def _spec(self, phase, case, condition, block, purpose="decision", **kwargs):
        key = request_key(self.study_id, phase, self.settings["model"], case, condition)
        return {"key": key, "phase": phase, "purpose": purpose, "block_id": block, **kwargs}

    async def wave(self, phase, specs, allow_unpriced_probe=False):
        if not specs:
            raise ValueError("Empty phase/wave")
        if self.config["provider"] == "together":
            specs = [
                {
                    **s,
                    "block_id": s["block_id"] + ":" + s["key"],
                    "metadata": {**s.get("metadata", {}), "submission_mode": "synchronous_singleton"},
                }
                for s in specs
            ]
        plan_path = self.stage / "manifest" / f"plan-{phase}.json"
        write_json(plan_path, specs, immutable=True)
        expected = defaultdict(list)
        for spec in specs:
            expected[spec["block_id"]].append(spec["key"])
        coordinator = Coordinator(
            self.journal,
            self.codec,
            self.transport,
            dict(expected),
            self.price,
            watchdog=self.config.get("watchdog_seconds", 30),
            poll_interval=self.config.get("poll_seconds", 5),
            study_limit=int(self.config["budget"]["study_cap_usd"] * 1_000_000)
            if "budget" in self.config
            else None,
        )
        self.coordinator = coordinator
        provider.ACTIVE_COORDINATOR = coordinator
        logdir = self.runtime / "inspect_logs" / self.study_id / phase
        self.context = SimpleNamespace(
            codec=self.codec,
            settings=self.settings,
            price=self.price,
            study_id=self.study_id,
            journal=self.journal,
            coordinator=coordinator,
        )
        try:
            if not allow_unpriced_probe:
                await coordinator.recover_pending(self.study_id)
            logs = await eval_async(
                audit_task(specs, self.context),
                model="g2g/" + self.settings["model"],
                max_retries=0,
                max_connections=len(specs),
                adaptive_connections=False,
                max_tokens=self.settings["max_output_tokens"],
                cache=False,
                max_samples=len(specs),
                log_dir=str(logdir),
                fail_on_error=False,
                score=False,
                log_model_api=True,
            )
            if any(log.status != "success" for log in logs):
                raise RuntimeError("Inspect wave incomplete; resume reattaches journal work")
            for spec in specs:
                row = self.journal.get(spec["key"])
                if not row or row["state"] not in (
                    ("Collected", "Unpriced") if allow_unpriced_probe else ("Collected",)
                ):
                    raise RuntimeError("Wave includes incomplete or unpriced member")
            # Only finalised official logs are published into the backup tree.
            for path in logdir.glob("*.eval"):
                atomic_write(
                    self.root / "runs" / "inspect_logs" / self.study_id / path.name,
                    path.read_bytes(),
                    immutable=True,
                )
            return {
                s["key"]: (
                    json.loads(self.journal.get(s["key"])["observation_json"])
                    if allow_unpriced_probe and self.journal.get(s["key"])["state"] == "Unpriced"
                    else self.journal.replay(s["key"])
                )
                for s in specs
            }
        finally:
            await coordinator.close()
            provider.ACTIVE_COORDINATOR = None

    async def close(self):
        if hasattr(self.transport, "close"):
            await self.transport.close()

    async def run(self, phase):
        readiness(self.root, self.config, phase)
        from g2g.validation import validate_inputs

        validate_inputs(self.root, self.config, phase)
        if phase == "dataset":
            return await self.dataset()
        if phase == "histories":
            return await self.histories()
        if phase == "state":
            return await self.state()
        return await self.audit_phase(phase)

    async def dataset(self):
        from g2g.ingest.pipeline import generate_dataset, inventory_sources

        cfg = self.config["dataset"]
        if cfg.get("source_manifest"):
            inventory = inventory_sources(
                json.loads((self.root / cfg["source_manifest"]).read_text(encoding="utf-8")), self.root
            )
            write_json(self.stage / "source_inventory.json", inventory, immutable=True)
        # Generation requests are explicitly staged: A registry must be reviewed
        # before B/D expansion; C gets only visible packets. Templates live in prompts.
        for stage in cfg.get("generation_stages", []):
            if stage["prompt"] in ("B", "D", "E") and not stage.get("registry_reviewed"):
                raise ValueError("Review family registry before generation")
            jobs = read_jsonl(self.root / stage["jobs"])
            specs = []
            for i, job in enumerate(jobs):
                if stage["prompt"] == "C" and any(
                    k in canonical(job).decode() for k in ('"gold"', '"label"', '"history"')
                ):
                    raise ValueError("Triage must be blinded to gold and histories")
                specs.append(
                    self._spec(
                        "dataset-" + stage["prompt"],
                        str(i),
                        "generation",
                        f"dataset-{i // 10}",
                        "generation",
                        messages=[
                            {
                                "role": "system",
                                "content": (self.root / "prompts" / f"{stage['prompt']}.txt").read_text(
                                    encoding="utf-8"
                                ),
                            },
                            {"role": "user", "content": json.dumps(job, sort_keys=True)},
                        ],
                        metadata={"prompt": stage["prompt"]},
                    )
                )
            results = await self.wave("dataset-" + stage["prompt"], specs)
            write_json(self.stage / f"generated-{stage['prompt']}.json", results, immutable=True)
        if cfg.get("generation_stages") and any(
            not (self.root / cfg[name]).exists() for name in ("registry", "candidates")
        ):
            return {"status": "needs_generated_output_review", "study_id": self.study_id}
        return generate_dataset(self.root, cfg)

    async def histories(self):
        scripts = _load(self.root, self.config, "scripts")
        if self.config["provider"] != "fixture" and len(scripts) != 60:
            raise ValueError("60 reviewed script pairs required")
        histories, turn1 = [], {}
        work = []
        for script in scripts:
            validate_script(script)
            binding = IdentityBinding.model_validate(script["binding"])
            bound = compile_identity_bindings(script, binding)["compiled"]
            for condition in ("neutral", "helpful"):
                work.append(
                    {
                        "script": bound,
                        "condition": condition,
                        "binding": binding.model_dump(),
                        "native": None,
                        "messages": [],
                        "dependency": [],
                    }
                )
        for turn in range(4):
            specs = []
            for i, item in enumerate(work):
                script, condition = item["script"], item["condition"]
                current = {"role": "user", "content": script[condition + "_turns"][turn]}
                spec = self._spec(
                    "histories",
                    script["pair_id"],
                    f"{condition}:{turn + 1}",
                    f"history-wave-{turn}-{i // 10}",
                    "history",
                    messages=[{"role": "system", "content": SYSTEM}] + item["messages"] + [current],
                    native_prefix=item["native"],
                    dependencies=item["dependency"],
                    metadata={"pair_id": script["pair_id"], "condition": condition, "turn": turn + 1},
                )
                specs.append(spec)
            results = await self.wave(f"history-turn-{turn + 1}", specs)
            for item, spec in zip(work, specs):
                observation = results[spec["key"]]
                if observation["status"] != "success":
                    raise ValueError(
                        "Technically invalid history retained; frozen bounded replacement review required"
                    )
                # The exact request actually compiled by the Inspect solver is authoritative.
                request = json.loads(self.journal.get(spec["key"])["request_json"])["payload"]
                item["native"] = self.codec.advance_history(request, observation["raw"])
                item["messages"] += [
                    spec["messages"][-1],
                    {"role": "assistant", "content": observation["final_text"]},
                ]
                item["dependency"] = [spec["key"]]
                record = {
                    "history_id": f"{item['script']['pair_id']}:{item['condition']}:{turn + 1}",
                    "pair_id": item["script"]["pair_id"],
                    "script_id": item["script"]["script_id"],
                    "condition": item["condition"],
                    "binding": item["binding"],
                    "model_id": self.settings["model"],
                    "turn": turn + 1,
                    "native_payload": item["native"],
                    "readable_messages": item["messages"].copy(),
                    "prefix_hash": digest(item["native"]),
                    "dependencies": item["dependency"],
                }
                write_json(
                    self.stage / "histories" / (digest(record["history_id"]) + ".json"),
                    record,
                    immutable=True,
                )
                if turn == 0 and item["condition"] == "helpful":
                    turn1[record["history_id"]] = record
                if turn == 3:
                    histories.append(record)
        write_json(self.stage / "histories" / "bank.json", histories, immutable=True)
        write_json(self.stage / "histories" / "turn1.json", turn1, immutable=True)
        write_json(
            self.stage / "histories" / "balance.json", audit_history_balance(histories), immutable=True
        )
        return {"histories": len(histories), "calls": len(work) * 4}

    def _cases(self):
        return _load(self.root, self.config, "cases")

    async def audit_phase(self, phase):
        cases = [
            c
            for c in self._cases()
            if c["split"] == {"calibrate": "calibration", "specificity": "main"}.get(phase, phase)
        ]
        assignments = _load(self.root, self.config, "assignments")
        assigned = {r["case_id"]: r for r in assignments}
        if phase in ("main", "specificity"):
            validate_assignments([assigned[c["case_id"]] for c in self._cases() if c["split"] == "main"])
        if phase == "specificity":
            selected = set(_load(self.root, self.config, "specificity"))
            cases = [c for c in cases if c["case_id"] in selected]
            if self.config["provider"] != "fixture" and (
                len(cases) != 150 or len({assigned[c["case_id"]]["history_pair_id"] for c in cases}) != 60
            ):
                raise ValueError("Specificity selection must cover 150 cases and all 60 pairs")
        bank = {}
        if phase in ("main", "specificity"):
            bank = {
                (h["pair_id"], h["condition"]): h
                for h in json.loads((self.stage / "histories" / "bank.json").read_text(encoding="utf-8"))
            }
            self.journal.lock_threshold(self.study_id, None)
            for history in bank.values():
                verify_history_record(history, self.journal, self.codec, self.settings["model"])
        specs = []
        if phase == "main":
            blocks = canonical_blocks(
                [assigned[c["case_id"]] for c in cases], self.settings["model"], self.config["seed"]
            )
            ordered = [(block, row["case_id"], row["arm"]) for block, rows in blocks.items() for row in rows]
        else:
            arms = (1, 2) if phase == "specificity" else (0,)
            ordered = [(f"{phase}-{i // 10}", c["case_id"], arm) for i, c in enumerate(cases) for arm in arms]
        lookup = {c["case_id"]: c for c in cases}
        for block, case_id, arm in ordered:
            case = lookup[case_id]
            assignment = assigned[case_id]
            history = (
                bank.get((assignment["history_pair_id"], "neutral" if arm == 1 else "helpful"))
                if arm in (1, 2)
                else None
            )
            if self.config["provider"] == "together":
                block += ":" + case_id + ":" + str(arm)
            specs.append(
                self._spec(
                    phase,
                    case_id,
                    arm,
                    block,
                    visible=case["visible"],
                    arm=arm,
                    binding=assignment["binding"],
                    history=history,
                    swap=phase == "specificity",
                    dependencies=history.get("dependencies", []) if history else [],
                    metadata={
                        "case_id": case_id,
                        "family_id": case["family_id"],
                        "source": case["source"]["type"],
                        "domain": case["domain"],
                        "arm": arm,
                        "history_pair_id": assignment["history_pair_id"],
                    },
                )
            )
        await self.wave(phase, specs)
        if phase == "calibrate":
            gold = {g["case_id"]: g for g in _load(self.root, self.config, "gold")}
            rows = []
            from g2g.analysis.parser import parse_decision

            for spec in specs:
                o = self.journal.replay(spec["key"])
                d = parse_decision(o["final_text"], o["status"])
                cid = spec["metadata"]["case_id"]
                rows.append(
                    {
                        **spec["metadata"],
                        "split": "calibration",
                        "label": gold[cid]["label"],
                        "status": d.status,
                        "score": d.score,
                    }
                )
            locked = fit_threshold(
                rows,
                self.config["calibration"]["source_weights"],
                self.config["calibration"]["expected_counts"],
            )
            self.journal.lock_threshold(self.study_id, locked)
            with self.journal.db:
                self.journal.event(
                    self.study_id,
                    None,
                    "fit_threshold",
                    "locked",
                    {"record_hash": digest(locked), "tau": locked["tau"]},
                )
        if phase == "pilot":
            import numpy as np
            from g2g.analysis.calibration import output_allowance

            obs = [self.journal.replay(s["key"]) for s in specs]
            tokens = [o["usage"]["output_tokens"] for o in obs if o["usage"].get("output_tokens") is not None]
            valid = sum(o["status"] == "valid" for o in obs) / len(obs)
            diagnostics = {
                "validity": valid,
                "validity_review_required": valid < 0.98,
                "output_cap_stops": sum(o["status"] == "truncated" for o in obs),
                "native_raw_bytes": sum(len(canonical(o["raw"])) for o in obs),
                "output_tokens_mean": float(np.mean(tokens)) if tokens else None,
                "output_tokens_q99": float(np.quantile(tokens, 0.99, method="higher")) if tokens else None,
                "automatically_passes_live_gate": False,
            }
            try:
                diagnostics["suggested_planning_allowance"] = output_allowance(
                    tokens, 75, self.settings["max_output_tokens"]
                )
            except ValueError as exc:
                diagnostics["allowance_review_required"] = str(exc)
            write_json(self.stage / "pilot-diagnostics.json", diagnostics, immutable=True)
        return {"phase": phase, "requests": len(specs)}

    async def state(self):
        if self.config["provider"] != "fixture":
            self.journal.lock_threshold(self.study_id, None)
        templates = _load(self.root, self.config, "state_templates")
        bank = json.loads((self.stage / "histories" / "turn1.json").read_text(encoding="utf-8"))
        for history in bank.values():
            verify_history_record(history, self.journal, self.codec, self.settings["model"])
        work = build_state_histories(templates, bank)
        # Generate genuine common Turn-2..4 suffixes, preserving the base exactly.
        for item in work:
            item["native"] = item["base"]["native_payload"]
            item["messages"] = item["base"]["readable_messages"].copy()
            item["dependencies"] = item["base"]["dependencies"]
        for turn in range(3):
            specs = []
            for i, item in enumerate(work):
                template = item["template"]
                actor = template["actor_turns_2_to_4"][turn]
                event = [
                    e
                    for e in item["trusted_events"]
                    if datetime.fromisoformat(e["event_time"])
                    == datetime.fromisoformat(template["turn_times"][turn])
                ]
                current = {
                    "role": "user",
                    "content": json.dumps(
                        {"evaluator_controlled_events": event, "untrusted_actor_text": actor}
                    ),
                }
                specs.append(
                    self._spec(
                        "state-suffix",
                        template["grant_template_id"],
                        turn + 2,
                        f"state-wave-{turn}-{i // 10}",
                        "history",
                        messages=[{"role": "system", "content": SYSTEM}] + item["messages"] + [current],
                        native_prefix=item["native"],
                        dependencies=item["dependencies"],
                        metadata={"grant_template_id": template["grant_template_id"], "turn": turn + 2},
                    )
                )
            results = await self.wave(f"state-suffix-{turn + 2}", specs)
            for item, spec in zip(work, specs):
                o = results[spec["key"]]
                if o["status"] != "success":
                    raise ValueError("State history technical failure; no case-specific prefix repair")
                native = json.loads(self.journal.get(spec["key"])["request_json"])["payload"]
                item["native"] = self.codec.advance_history(native, o["raw"])
                item["messages"] += [spec["messages"][-1], {"role": "assistant", "content": o["final_text"]}]
                item["dependencies"] = [spec["key"]]
        specs, contexts = [], []
        for item in work:
            template, base = item["template"], item["base"]
            history = {
                "binding": base["binding"],
                "native_payload": item["native"],
                "readable_messages": item["messages"],
                "prefix_hash": digest(item["native"]),
            }
            write_json(
                self.stage / "histories" / ("state-" + digest(template["grant_template_id"]) + ".json"),
                history,
                immutable=True,
            )
            for c in template["cases"]:
                # Bound current fields only; original templated packet is needed by audit compiler.
                original = next(
                    t for t in templates if t["grant_template_id"] == template["grant_template_id"]
                )
                visible = next(x for x in original["cases"] if x["case_id"] == c["case_id"])["visible"]
                spec = self._spec(
                    "state-extract",
                    c["case_id"],
                    "extract",
                    f"extract-{c['case_id']}",
                    "extraction",
                    messages=extraction_prompt(history, c["visible"]),
                    dependencies=item["dependencies"],
                    metadata={
                        "case_id": c["case_id"],
                        "family_id": template["family_id"],
                        "grant_template_id": template["grant_template_id"],
                        "base_prefix_id": template["base_prefix_id"],
                        "binding": base["binding"],
                    },
                )
                specs.append(spec)
                contexts.append((visible, base["binding"], history, item["dependencies"]))
        outputs = await self.wave("state-extract", specs)
        decisions = []
        for spec, (visible, binding, history, deps) in zip(specs, contexts):
            extracted = outputs[spec["key"]]
            ledger, error = validate_extracted_state(extracted["final_text"], extracted["status"])
            if error:
                ledger = {"extraction_status": "invalid", "reason": error}
            with self.journal.db:
                self.journal.event(
                    self.study_id,
                    spec["key"],
                    "extract_state",
                    "invalid" if error else "valid",
                    {"reason": error, "observation_id": extracted["observation_id"]},
                )
            for protocol, arm in [("full", 2), ("structured", 0), ("reset", 0)]:
                cid = spec["metadata"]["case_id"]
                decisions.append(
                    self._spec(
                        "state",
                        cid,
                        protocol,
                        f"state-{cid}",
                        visible=visible,
                        arm=arm,
                        binding=binding,
                        history=history if protocol == "full" else None,
                        ledger=ledger if protocol == "structured" else None,
                        dependencies=deps + [spec["key"]],
                        metadata={
                            **spec["metadata"],
                            "protocol": protocol,
                            "extraction_valid": error is None,
                            "extraction_observation_id": extracted["observation_id"],
                        },
                    )
                )
        await self.wave("state-decisions", decisions)
        return {"suffix_calls": 90, "extraction_calls": 150, "monitor_calls": 450}
