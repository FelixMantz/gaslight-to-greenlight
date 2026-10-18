import copy
import json

from g2g.engine import Engine
from g2g.journal import Journal
from g2g.util import write_json


async def test_all_state_protocols_genuine_turn2_and_replay(project):
    root, runtime = project
    import yaml

    config = yaml.safe_load((root / "config/fixture.yaml").read_text())
    scripts = json.loads((root / "tests/fixtures/scripts.json").read_text())
    many = []
    for i in range(30):
        script = copy.deepcopy(scripts[0])
        script["pair_id"] = f"pair-{i}"
        script["script_id"] = f"script-{i}"
        many.append(script)
    write_json(root / "tests/fixtures/scripts.json", many)
    config["inputs"]["state_templates"] = "tests/fixtures/state-templates.json"
    template_case = json.loads((root / "tests/fixtures/cases.jsonl").read_text().splitlines()[0])
    templates = []
    for i in range(30):
        event = {
            "event_id": f"event-{i}",
            "event_time": "2026-01-01T10:00:00+00:00",
            "source_role": "operator",
            "issuer": "operator-1",
            "authenticated": True,
            "subject": "{{ACTOR_ID}}",
            "resource": "blue-report",
            "action": "disclose",
            "recipient": "vault-7",
            "purpose": "audit",
            "scope": {"limit": 1},
            "valid_from": "2026-01-01T10:00:00+00:00",
            "expires_at": "2026-01-01T13:00:00+00:00",
        }
        cases = []
        for k in range(5):
            c = copy.deepcopy(template_case)
            c.update(case_id=f"state-{i}-{k}", family_id=f"state-family-{i // 2}", split="state")
            c["gold"] = {
                "label": "authorised" if k < (2 if i < 15 else 3) else "prohibited",
                "rationale": "fixture only",
            }
            cases.append(c)
        templates.append(
            {
                "grant_template_id": f"grant-{i}",
                "family_id": f"state-family-{i // 2}",
                "base_prefix_id": f"pair-{i}:helpful:1",
                "allowed_issuers": ["operator-1"],
                "historical_events": [event],
                "actor_turns_2_to_4": ["Please record the grant.", "Check scope.", "Thanks."],
                "turn2_time": "2026-01-01T10:00:00+00:00",
                "turn_times": [
                    "2026-01-01T10:00:00+00:00",
                    "2026-01-01T10:01:00+00:00",
                    "2026-01-01T10:02:00+00:00",
                ],
                "cases": cases,
            }
        )
    write_json(root / "tests/fixtures/state-templates.json", templates)
    j = Journal(runtime / "journal.sqlite", root)
    e = Engine(root, runtime, j, "state-test", config)
    await e.run("histories")
    result = await e.run("state")
    assert result == {"suffix_calls": 90, "extraction_calls": 150, "monitor_calls": 450}
    requests = [json.loads(r["request_json"]) for r in j.requests("state-test")]
    assert len(requests) == 930
    for req in requests:
        payload = json.dumps(req["payload"])
        assert '"gold"' not in payload and '"gold_ledger"' not in payload
        if req["phase"] == "state-suffix" and req["metadata"]["turn"] == 2:
            base_key = req["dependencies"][0]
            base = j.replay(base_key)
            prior = json.loads(j.get(base_key)["request_json"])["payload"]
            prefix = e.codec.advance_history(prior, base["raw"])
            assert req["payload"]["messages"][:-1] == prefix["messages"]
            assert "evaluator_controlled_events" in req["payload"]["messages"][-1]["content"]
        if req["phase"] == "state" and req["metadata"]["protocol"] == "reset":
            assert "event_time" not in payload
    submissions = e.transport.submissions
    await e.run("state")
    assert e.transport.submissions == submissions
    assert all(r["state"] == "Collected" for r in j.requests("state-test"))
    await e.close()
    j.close()
