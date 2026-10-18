import json
import httpx
import pytest

from g2g.adapters.transport import ProviderTransport
from g2g.models import Request


def request(provider="openai"):
    return Request(
        key="a",
        study_id="s",
        phase="pilot",
        model_id="pinned",
        purpose="probe",
        block_id="b",
        payload={"model": "pinned", "input": []},
        config_hash="x",
        planning_microusd=1,
        maximum_microusd=2,
    )


@pytest.mark.parametrize(
    "provider,env",
    [("openai", "OPENAI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY"), ("google", "GOOGLE_API_KEY")],
)
async def test_native_submit_collect_custom_ids(provider, env, tmp_path, monkeypatch):
    monkeypatch.setenv(env, "offline-test-placeholder")
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        path = req.url.path
        if provider == "openai":
            if path.endswith("/files"):
                return httpx.Response(200, json={"id": "file-1"})
            if req.method == "POST":
                body = json.loads(req.content)
                assert body["endpoint"] == "/v1/responses" and body["completion_window"] == "24h"
                return httpx.Response(200, json={"id": "batch-1"})
            if path.endswith("/batches/batch-1"):
                return httpx.Response(200, json={"status": "completed", "output_file_id": "output-1"})
            return httpx.Response(
                200, text=json.dumps({"custom_id": "a", "response": {"body": {"output": [], "usage": {}}}})
            )
        if provider == "anthropic":
            if req.method == "POST":
                body = json.loads(req.content)
                assert body["requests"][0]["custom_id"] == "a"
                return httpx.Response(200, json={"id": "batch-1"})
            if path.endswith("/results"):
                return httpx.Response(
                    200,
                    text=json.dumps(
                        {"custom_id": "a", "result": {"type": "succeeded", "message": {"content": []}}}
                    ),
                )
            return httpx.Response(200, json={"processing_status": "ended"})
        if req.method == "POST":
            body = json.loads(req.content)
            assert body["batch"]["input_config"]["requests"]["requests"][0]["metadata"]["key"] == "a"
            return httpx.Response(200, json={"name": "batches/batch-1"})
        return httpx.Response(
            200,
            json={
                "done": True,
                "response": {
                    "inlinedResponses": [{"metadata": {"key": "a"}, "response": {"candidates": []}}]
                },
            },
        )

    client = httpx.AsyncClient(base_url="https://offline.invalid/v1/", transport=httpx.MockTransport(handler))
    transport = ProviderTransport(provider, tmp_path, client)
    batch = await transport.submit([request()])
    result = await transport.poll(batch, ["a"])
    assert set(result) == {"a"}
    assert (
        sum(
            method == "POST" and ("batches" in path or ":batchGenerateContent" in path)
            for method, path in calls
        )
        == 1
    )
    await transport.close()


async def test_ambiguous_post_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-placeholder")
    count = 0

    def handler(req):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("Acceptance may have happened")

    client = httpx.AsyncClient(base_url="https://offline.invalid/", transport=httpx.MockTransport(handler))
    transport = ProviderTransport("anthropic", tmp_path, client)
    with pytest.raises(httpx.ReadTimeout):
        await transport.submit([request()])
    assert count == 1
    await transport.close()
