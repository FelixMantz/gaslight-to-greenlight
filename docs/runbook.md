# Laptop operations

## Setup

Clone the private repository to the laptop. Install the frozen uv environment and run the offline example. Record the OS, filesystem, available disk space, native-state sizes and fsync timings during pilot setup. Verify that the OS-local runtime directory is outside *all* active sync roots, and that the project directory is included in the existing backup setup.

Put provider keys in OS-managed environment variables or an untracked local secret file loaded by your shell. The program does not silently load `.env`. Never add API keys, runtime journals, raw study data or evaluation logs to Git.

Create separate untracked presets under `data/config/`. Start from `config/study.yaml` for reviewed collection or `config/dataset.yaml` for staged preparation. A pilot can use its own smaller input files/preset before the full study is frozen. Prices are USD per million tokens for the **actual route**, with a dated `version`; verify batch/cache-write/read treatment separately. Together remains a synchronous single-request path until account-specific batch and native replay support is established. Set its actual hosted model ID and quantisation after verification.

All gate entries have this form:

```yaml
gates:
  endpoint_verified:
    passed: true
    evidence_file: data/capabilities/openai-endpoint.json
```

The evidence JSON must contain `passed: true`, a `config_hash` matching `g2g.freeze.capability_config_hash(preset)`, and the reviewer-owned evidence explaining the conclusion. This is a scientific signoff record, not an automatic proof. Its hash is frozen with the study. Required gate names are in `freeze.readiness()`; confirmatory phases add pilot, replay, dataset, source, script, protocol and simulation gates.

Keep the source SHA, package environment, prompts, cases, labels, assignments, weights and gates unchanged after freeze. `resume` verifies their hashes. A changed configuration requires a new named study and an explicitly reviewed protocol decision. Provider, model settings and prices are all included in the capability evidence hash. Tracked and untracked source changes are rejected on resume, even if HEAD has not moved.

## Inspect and scheduling

The solver compiles one native payload per request attempt. The planner supplies membership, not another independently compiled prompt. Every wave admits all its samples with static Inspect concurrency at least its size; a small independent semaphore limits HTTP work. A watchdog fails incomplete blocks without waiting for evaluation completion. Failed sample compilation stops the block rather than hanging other samples.

History turns advance only after preceding replies are archived. Main matched blocks contain five actual history pairs and both conditions, using exact assigned membership. Caps can stop a block before submission. Re-plan smaller **frozen** blocks before the run if the cost/token/provider limit is too large; do not edit a running plan or bypass a cap. Together's synchronous route splits to singleton chunks and therefore records a different timing mode.

## Pause and resume

```bash
g2g report STUDY
g2g resume STUDY
```

`resume --phase histories` or another explicit phase restarts that phase through normal Inspect logging. Already collected answers replay with original observation IDs/times/usage and `origin=journal_replay`. New Inspect logs may show historical token usage; use the journal, not summed viewer totals, for billing.

Provider poll failures leave accepted IDs unchanged. Correct connectivity and resume. Inspect's automatic create retries are disabled. Provider refusal, malformed final answer or truncation is a retained observation and deployed escalation, never a request for a nicer answer.

## Acceptance and billing reconciliation

A request left in Reserved when the journal reopens becomes Unknown. Search provider listings/metadata and billing using the saved request/chunk/custom IDs and timestamps. Preserve the supporting export/evidence locally.

```bash
g2g reconcile KEY1 KEY2 --batch-id EXISTING_PROVIDER_ID --evidence "Reviewed provider export at ..."
```

Only use `--definitely-unsent` when provider evidence establishes non-acceptance. A missing local ID does not establish that. When no safe reconciliation exists, keep the reservation and stop.

A terminal failed/expired item with no billed usage becomes Unpriced. Reconcile its actual charge before continuing:

```bash
g2g reconcile-usage KEY ACTUAL_MICROUSD --evidence "Provider billing export ..."
g2g charge EXTERNAL_CHARGE_ID AMOUNT_MICROUSD --description "Billable dataset preparation/storage ..."
```

One dollar is 1,000,000 micro-USD. Charge IDs deduplicate separate charges. The original raw observation remains immutable; reconciliation is an additional event and journal charge value. Exceeding a verified maximum or the invalid-output technical-pause rule blocks further submissions. Review the configuration/protocol; there is no automatic override button.

## Restore from backup

Stop the runner and keep the current runtime intact for forensic comparison. Restore the snapshot **and every referenced immutable object**, never just a `.eval` log. Verify SQLite integrity and archive hashes. Copy the snapshot to a new non-synced runtime as `journal.sqlite` while no process has it open. Do not restore live WAL/SHM sidecars from independent sync timestamps.

A snapshot can predate a later submission that is absent from it. Compare provider batch listings, metadata and charges against the restored request inventory before paid work, record that audit and any additional spend. The software cannot infer a provider submission whose entire local intent record was lost in an older backup. Read-only reporting is safe while this audit is unresolved. Do not interpret a restored Planned state as proof that no later call was accepted.

## Analysis and export

Seal only at the frozen analysis point after all specified collection/charges are complete. Technical status does not require sealing. Offline debug re-parses saved final answers:

```bash
g2g report STUDY --debug-key KEY
g2g seal-analysis STUDY
g2g report STUDY --analysis
g2g export PATH_TO_OFFICIAL.eval reports/text-audit-export.json
```

The exporter uses the official Inspect reader and labels its versioned text-audit mapping honestly. It does not fabricate a ControlArena environment/tool trajectory. A native ControlArena importer must be separately matched to a pinned supported contract before claiming that compatibility.
