# Dataset and provenance contracts

No completed research dataset is bundled. The tiny fictional fixtures deliberately repeat a simple policy and are not evidence of independent semantic families. The real design targets 50 pilot, 500 calibration, 450 Main and 150 state cases, with disjoint semantic/upstream-task families and the reviewed source allocations in architecture v6.

## Preparation

`g2g run --phase dataset --preset config/dataset.yaml` consumes a reviewed family registry and UTF-8 JSONL candidate records, retaining provisional `gold` separately. A `.txt` extension is accepted for manual imports. See `tests/fixtures/candidates.jsonl` for the record shape, not content suitable for study use.

The source inventory requires full immutable revision SHAs, explicit selected paths/SHA256 hashes, attribution and applicable terms. MRT has independently pinned GitHub code and Hugging Face dataset revisions; record CC-BY-NC-4.0 terms and upstream distinctions. `download_hf_files()` fetches only explicitly selected revision-pinned files. AgentDojo/source traces must be inventoried from the actual downloaded pinned tree. No live rollout is inferred from a task definition.

For adapted cases, use a reviewed mapping containing:

```json
{
  "record_index": 0,
  "reviewed_pre_action": true,
  "evidence_spans": [{"field":"transcript", "start":0, "end":5, "text":"exact"}],
  "case": {"case_id":"...", "family_id":"...", "split":"main", "domain":"disclosure", "source":{"type":"adapted"}, "visible":{}},
  "provisional_gold": {"label":"violating", "rule_ids":["R1"], "evidence_ids":["E1"], "rationale":"...", "review_status":"unreviewed"},
  "reconstructed_policy": true
}
```

The example's empty `visible` must be replaced by a valid `VisiblePacket`. Group mappings by the inventory's exact source path. The ingester checks quoted spans against the actual source record. Review pre-action chronology, trust roles, omitted evidence, policy reconstruction and semantic duplication; automated field-name stripping is not a validity argument.

Upstream notices/canaries belong in provenance metadata. They never become JSON comment lines or model prompt headers. SHA256 verifies bytes; a canary does not verify labels or prevent contamination.

## Generation stages

`dataset.generation_stages` accepts explicit jobs for prompts A–E. Each job is a user payload; batches group ten jobs (each job should request the frozen bounded 10–20 records). All generator calls use the same journal/caps as study work. Registry expansion is gated on `registry_reviewed` for B/D/E. C rejects payloads with gold/label/history fields. Raw generated responses remain archived, including malformed outputs; they are not automatically accepted as gold.

Import the accepted JSONL records into `dataset.candidates` for schema/leakage checks and the review queue. Prompt A produces a proposed family registry that must be reviewed before expansion. The supplied prompts deliberately allow the generator to stop if diversity is infeasible. Automatic generation does not certify source provenance, licences, semantic diversity or correctness.

Every case uses `{{ACTOR_ID}}` and, when needed, `{{OTHER_ACTOR_ID}}`. Only these literal placeholders are substituted in parsed string values. There is no executable/general template language. Fixed names with role-dependent authorisation need explicit human review before specificity eligibility.

## Review and freeze

The Markdown review queue contains only case ID and visible fields, in seeded order. Provisional labels, histories and model outcomes are omitted. Record the human result in a separate CSV:

`case_id,reviewer,judgement,rule_ids,evidence_ids,rationale,ambiguous,second_pass,adjudicated`

Separate multiple rule/evidence IDs with semicolons. Review every case. Freeze `second_review_ids` as a stratified sample before confirmatory outcomes; those cases and ambiguous cases require a second reviewer or a recorded separate blinded pass. Disclose use of a single person's separate passes. Adjudicated human judgement can correct provisional generator gold. Revise/remove unresolved cases before setting `dataset.freeze: true`; do not select cases by later treatment effects.

Freeze writes `data/frozen/cases.jsonl`, `gold.jsonl` and a hash manifest. File identity is immutable: edit source candidates/reviews and use a new reviewed freeze destination for a revised protocol rather than overwriting a frozen dataset. Cases/gold remain separate all the way into the model compiler and Inspect samples.

## Histories and state inputs

Each actor script has `pair_id`, `script_id`, `reviewed`, `binding`, `neutral_turns[4]`, `helpful_turns[4]` and matching notes. Counterbalance distinct benefactor/unrelated names before generating responses. Technical history failures remain archived; extra length or indebtedness is descriptive, not a selection filter.

State templates extend Prompt D with the scheduling fields `base_prefix_id`, `allowed_issuers`, `turn2_time` and `turn_times[3]`. Use 30 distinct helpful Turn-1 prefixes. `historical_events` and `actor_turns_2_to_4` are common across the five variants. Put differing claims only in each current packet. Trusted events must pass `schemas/TrustedEvent.schema.json`; actor strings cannot create trusted envelopes. Freeze reviewed state cases/gold separately and include them in the same case/gold input files. Each shared template belongs to one state family. The coarsest family controls clustered uncertainty when templates share a parent.
