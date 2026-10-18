"""Exercise official Inspect logs, phase dependencies, journal replay and HTML safety."""

import json

import yaml
from inspect_ai.log import read_eval_log

from g2g.engine import Engine
from g2g.freeze import freeze_study
from g2g.journal import Journal
from g2g.reporting import report


async def test_fixture_run_report_resume_inspect(project):
    root, runtime = project
    config = yaml.safe_load((root / "config/fixture.yaml").read_text())
    j = Journal(runtime / "journal.sqlite", root, "test")
    j.freeze("integration", freeze_study(root, config))
    e = Engine(root, runtime, j, "integration", config)
    await e.run("pilot")
    await e.run("histories")
    await e.run("calibrate")
    await e.run("main")
    await e.run("specificity")
    count = e.transport.submissions
    prior = j.spending()
    # Restart each completed phase via the same public orchestration path.
    await e.run("main")
    assert e.transport.submissions == count
    assert j.spending() == prior
    logs = list((root / "runs/inspect_logs/integration").glob("*.eval"))
    assert logs
    log = read_eval_log(str(logs[0]))
    assert log.status == "success" and log.samples
    assert all(not s.target for s in log.samples)
    assert "gold" not in json.dumps(log.samples[0].metadata)
    result = report(j, "integration", root, analysis=True)
    assert result["observations"] == 71  # pilot 5 + histories 16 + calibration 5 + main 35 + specificity 10
    html = (root / "reports/integration/index.html").read_text()
    assert "<alice@example.com>" not in html and "&lt;alice@example.com&gt;" in html
    assert (root / "reports/integration/analysis.json").exists()
    await e.close()
    j.close()


async def test_together_all_wave_types_use_singleton_transport(project, monkeypatch):
    import httpx
    from g2g.adapters.transport import ProviderTransport

    root, runtime = project
    config = yaml.safe_load((root / "config/fixture.yaml").read_text())
    config["provider"] = "together"
    monkeypatch.setenv("TOGETHER_API_KEY", "offline-test-placeholder")
    j = Journal(runtime / "journal.sqlite", root)
    e = Engine(root, runtime, j, "sync-test", config)
    await e.transport.close()
    calls = []

    def handler(request):
        data = json.loads(request.content)
        calls.append(data)
        return httpx.Response(
            200,
            json={
                "id": str(len(calls)),
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "response",
                            "reasoning_content": "opaque",
                        },
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = httpx.AsyncClient(base_url="https://offline.invalid/", transport=httpx.MockTransport(handler))
    e.transport = ProviderTransport("together", root / "runs/sync-test/native_sync", client)
    specs = [
        e._spec(
            "histories",
            str(i),
            "turn1",
            "shared-block",
            "history",
            messages=[{"role": "user", "content": str(i)}],
        )
        for i in range(2)
    ]
    outputs = await e.wave("sync-probe", specs)
    assert len(calls) == 2 and len(outputs) == 2
    assert len({r["batch_id"] for r in j.requests("sync-test")}) == 2
    for request in j.requests("sync-test"):
        meta = json.loads(request["request_json"])["metadata"]
        assert meta["submission_mode"] == "synchronous_singleton"
    await e.close()
    j.close()
