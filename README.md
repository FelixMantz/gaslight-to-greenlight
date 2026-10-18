# Gaslight to Greenlight

A local Python harness for **Prior Collaboration and Fabricated Rapport in LLM Safety Monitoring**, implementing [systems architecture v6](docs/systems-architecture.md). Inspect AI writes the evaluation logs. A project-wide SQLite journal owns request identity, provider acceptance, raw observations and spending.

This is a **text-audit component study**. It executes no benchmark agents or proposed real-world actions. Included examples are fictional offline fixtures, not reviewed study data or experimental findings.

## Install and run the offline example

Use Python 3.11 or 3.12 and [uv](https://docs.astral.sh/uv/). Python 3.13 is allowed by the package contract; the CI matrix exercises 3.11 and 3.12 on Linux and Windows.

```bash
uv sync --frozen --extra dev
uv run --frozen pytest -q
uv run --frozen g2g run --phase pilot --preset fixture --study-id demo
uv run --frozen g2g run --phase histories --preset fixture --study-id demo
uv run --frozen g2g run --phase calibrate --preset fixture --study-id demo
uv run --frozen g2g run --phase main --preset fixture --study-id demo
uv run --frozen g2g run --phase specificity --preset fixture --study-id demo
uv run --frozen g2g report demo --analysis
uv run --frozen g2g resume demo --phase main
```

Open `reports/demo/index.html`. The example creates 71 observations with zero API calls. Repeating/resuming Main reuses its saved observations without charging or regenerating answers. The fixture threshold, scores and report are demonstration data, never scientific evidence.

Open the official archived `.eval` logs locally:

```bash
uv run --frozen inspect view --no-trust-content --log-dir runs/inspect_logs/demo
```

Inspect View is a local application. Keep it on loopback. The HTML report escapes all response text, including explanations containing markup.

## Runtime and recovery

By default the live journal is at `~/.local/share/g2g/journal.sqlite` (Windows: `%LOCALAPPDATA%/g2g/journal.sqlite`). Set `G2G_RUNTIME_DIR` to another **non-synced** local directory if required. The CLI rejects a runtime inside the project or a recognisable Dropbox/OneDrive/iCloud/Google Drive path; verify any other sync software yourself during laptop setup. Use the same runtime for all four model slots so the cap is project-wide.

Only one runner owns the OS file lock. Reports use a read-only journal connection and can run alongside it. Completed raw objects and Inspect logs go under ignored `runs/`. `runs/journal_snapshot.sqlite` is a consistent SQLite backup built at a temporary path, integrity checked, fsynced and atomically published. Existing laptop Dropbox backup can sync these completed objects; the harness does not call Dropbox.

Ctrl+C stops new work and snapshots the journal. Provider batches already accepted can keep running. On restart:

- **Collected:** verify the raw hash and reuse the original observation, including invalid output.
- **Submitted:** poll the saved provider ID; never submit a replacement.
- **Unknown acceptance:** keep both reservations and block paid submissions until reconciled.
- **Known unsent:** submit only after hashes, dependency, budget and block checks pass.

See [the recovery runbook](docs/runbook.md), including restoration of an older Dropbox snapshot. Exactly-once provider billing is not promised across an acceptance-before-local-commit crash.

## Scientific workflow

The CLI supports `dataset`, `pilot`, `histories`, `calibrate`, `main`, `specificity` and `state`. The full planned panel is 20,480 calls before pilot, data generation, probes and diagnostics. A run uses one exact provider configuration; use separate study IDs/presets and independent assignment seeds for the four model slots, sharing the project ledger.

1. Inventory pinned upstream source files and terms. Review semantic family allocation before expanding it. Use the five [frozen generation/review prompts](prompts/) or import UTF-8 `.jsonl`/`.txt` records.
2. `g2g run --phase dataset --preset config/dataset.yaml` validates/imports candidates and makes a blinded Markdown review queue plus a separate CSV. Resolve disputes and freeze reviewed cases/gold separately. See [dataset contracts](docs/dataset.md).
3. Run the separate small pilot/capability preset on the laptop. Verify native-state round-trip, four-turn continuation, prefix-mutation negative controls, finite token/cost bounds, output validity and prices. `--probe-replay` records probes; it does **not** automatically certify them.
4. Prepare independent model assignments with `g2g assign --cases data/frozen/cases.jsonl --gold data/frozen/gold.jsonl --scripts data/frozen/scripts.json --seed 841 --output data/frozen/model-1`. Repeat with other preregistered seeds for other models. All 60 pairs, five violating cases per pair, two/three benign, source/domain balance and unique family × pair cells are constrained. Specificity selects 150 cases spanning all pairs and both classes per pair.
5. Freeze the reviewed configuration and inputs. Generate core histories, calibrate one source-weighted Arm 0 threshold, then collect Main and specificity. Generate 30 separate genuine Turn-2 state suffixes, 150 extractor calls and 450 state-monitor calls through `--phase state`.
6. `g2g seal-analysis STUDY` checks frozen collection and reconciliation before permitting confirmatory output. `g2g report STUDY --analysis` gives model results. `g2g panel-report STUDY1 STUDY2 STUDY3 STUDY4 --output reports/panel.json` applies the single eight-test Holm family across the four slots.

`config/study.yaml` is a **locked-off template**, not permission to run a paid study. Exact model access, Together checkpoint/quantisation, pricing, replay fidelity, actual data diversity and human review cannot be established by fixture tests. Gate evidence is tied to a configuration hash. No older model is silently substituted. No paid collection has been performed to create this repository.

## Implementation

| Component | Location |
| --- | --- |
| CLI, freeze, gates and input validation | `src/g2g/cli.py`, `freeze.py`, `validation.py` |
| WAL journal, OS lock, cap reservations and atomic snapshots | `src/g2g/journal.py` |
| Canonical block barriers, shared batch pollers and recovery | `src/g2g/coordinator.py` |
| Inspect ModelAPI, raw model calls and official logs | `src/g2g/inspect_tasks/` |
| OpenAI/Anthropic/Google native batches; Together synchronous transport | `src/g2g/adapters/` |
| Native reasoning/signature preservation and replay probes | `adapters/codecs.py`, `probes.py` |
| Case/gold/identity/request/event/observation JSON Schemas | `schemas/` |
| Source inventory, pre-action span mapping and blinded review | `src/g2g/ingest/` |
| Seven arms, four history waves, specificity and state pipelines | `engine.py`, `planner.py`, `state.py` |
| Parser, weighted threshold, multiway and state inference | `src/g2g/analysis/` |
| Offline traces, HTML, panel analysis and honest text-audit export | `reporting.py`, `operations.py` |

The $450 operational stop uses planning reservations. The $500 exposure guard uses finite API caps and verified conservative input bounds. Both include unknown acceptance, unresolved usage and separately recorded preparation/development charges. Missing billed usage keeps its reservation until explicit reconciliation. No cache savings are assumed in planning. Inspect response caching and automatic ambiguous-create retries are disabled.

The inference library includes two-way finite-cluster sandwich variance with the IID floor, one-way coarsest-parent state variance, bounded missing-score sensitivities, direct source interaction, family threshold resampling, uncorrected pigeonhole and Gaussian REML sensitivity helpers. Simulations diagnose implementation and the eventual frozen assignment; they do not establish study power or finite-sample validity by themselves. [Analysis details](docs/analysis.md).

Tests cover the complete small core workflow, a 930-call fake state workflow and its replay, full 450-case assignment/specificity constraints, provider mock contracts, parser edge cases, budget/recovery failures and inference diagnostics. CI runs offline with no model secrets. [Verification and remaining research gates](docs/verification.md).
