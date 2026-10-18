from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from g2g.util import canonical, digest, write_json


class FixtureTransport:
    """Disk-backed fake batch service survives runner restarts and never uses network."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.submissions = 0

    async def submit(self, requests):
        self.submissions += 1
        batch_id = "fixture-" + digest([r.key for r in requests])[:24]
        results = {}
        for req in requests:
            if req.purpose == "decision":
                score = 5 + int(digest(req.payload)[:5], 16) % 90
                text = (
                    "<decision><explanation>Fixture assessment: R&D; x < y; "
                    "<alice@example.com>.</explanation><recommendation>allow</recommendation>"
                    f"<suspicion_score>{score}</suspicion_score></decision>"
                )
            elif req.purpose == "extraction":
                text = '{"grants":[],"rejected_claims":[],"fixture_only":true}'
            elif req.purpose == "history":
                text = "I checked the fictional task facts. Here is my response."
            else:
                text = "{}"
            results[req.key] = {
                "text": text,
                "usage": {"input_tokens": 100, "output_tokens": 80},
                "id": batch_id,
            }
        write_json(self.directory / (batch_id + ".json"), results, immutable=True)
        return batch_id

    async def poll(self, batch_id, keys):
        result = json.loads((self.directory / (batch_id + ".json")).read_text())
        if set(result) != set(keys):
            raise ValueError("Fixture custom ID mismatch")
        return result


class ProviderTransport:
    """Native batch APIs; HTTP creates have no retries. Poll GETs can be resumed.

    Together uses synchronous chat completions until its actual batch/replay
    capabilities are verified. A lost synchronous response is unknown acceptance.
    """

    def __init__(self, provider: str, spool: Path, client=None):
        self.provider, self.spool = provider, spool
        key_env = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "google": "GOOGLE_API_KEY",
            "together": "TOGETHER_API_KEY",
        }
        key = os.environ.get(key_env[provider])
        if not key:
            raise ValueError(f"Missing {key_env[provider]}")
        base = {
            "openai": "https://api.openai.com/v1",
            "anthropic": "https://api.anthropic.com/v1",
            "google": "https://generativelanguage.googleapis.com/v1beta",
            "together": "https://api.together.xyz/v1",
        }[provider]
        headers = {"Authorization": "Bearer " + key}
        if provider == "anthropic":
            headers = {
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "thinking-binding-controls-2026-08-01",
            }
        if provider == "google":
            headers = {"x-goog-api-key": key}
        self.client = client or httpx.AsyncClient(
            base_url=base + "/", headers=headers, timeout=120, transport=httpx.AsyncHTTPTransport(retries=0)
        )

    async def close(self):
        await self.client.aclose()

    async def _request(self, method, path, **kwargs):
        response = await self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response

    async def submit(self, requests):
        p = self.provider
        correlation = digest([r.key for r in requests])
        if p == "openai":
            lines = (
                b"\n".join(
                    canonical(
                        {"custom_id": r.key, "method": "POST", "url": "/v1/responses", "body": r.payload}
                    )
                    for r in requests
                )
                + b"\n"
            )
            upload = (
                await self._request(
                    "POST",
                    "files",
                    data={"purpose": "batch"},
                    files={"file": ("requests.jsonl", lines, "application/jsonl")},
                )
            ).json()
            created = (
                await self._request(
                    "POST",
                    "batches",
                    json={
                        "input_file_id": upload["id"],
                        "endpoint": "/v1/responses",
                        "completion_window": "24h",
                        "metadata": {"g2g_chunk": correlation},
                    },
                )
            ).json()
            return created["id"]
        if p == "anthropic":
            created = (
                await self._request(
                    "POST",
                    "messages/batches",
                    json={"requests": [{"custom_id": r.key, "params": r.payload} for r in requests]},
                )
            ).json()
            return created["id"]
        if p == "google":
            models = {r.payload["model"] for r in requests}
            if len(models) != 1:
                raise ValueError("Google batch must use one model")
            model = next(iter(models)).removeprefix("models/")
            created = (
                await self._request(
                    "POST",
                    f"models/{model}:batchGenerateContent",
                    json={
                        "batch": {
                            "display_name": "g2g-" + correlation,
                            "input_config": {
                                "requests": {
                                    "requests": [
                                        {
                                            "request": {k: v for k, v in r.payload.items() if k != "model"},
                                            "metadata": {"key": r.key},
                                        }
                                        for r in requests
                                    ]
                                }
                            },
                        }
                    },
                )
            ).json()
            return created["name"]
        if len(requests) != 1:
            raise ValueError("Together synchronous path admits one request per chunk")
        req = requests[0]
        raw = (await self._request("POST", "chat/completions", json=req.payload)).json()
        native_id = raw.get("id")
        if not native_id:
            raise ValueError("Accepted synchronous response missing request ID")
        batch_id = "sync-" + digest(native_id)
        write_json(self.spool / (batch_id + ".json"), {req.key: raw}, immutable=True)
        return batch_id

    async def poll(self, batch_id, keys):
        p = self.provider
        if p == "together":
            return json.loads((self.spool / (batch_id + ".json")).read_text())
        if p == "openai":
            batch = (await self._request("GET", "batches/" + batch_id)).json()
            if batch["status"] not in {"completed", "expired", "cancelled", "failed"}:
                return None
            result = {}
            for field in ("output_file_id", "error_file_id"):
                if batch.get(field):
                    content = (await self._request("GET", "files/" + batch[field] + "/content")).text
                    for line in content.splitlines():
                        row = json.loads(line)
                        key = row["custom_id"]
                        if key in result:
                            raise ValueError("Duplicate provider custom ID")
                        result[key] = row.get("response", {}).get("body") or {
                            "error": row.get("error") or row
                        }
        elif p == "anthropic":
            batch = (await self._request("GET", "messages/batches/" + batch_id)).json()
            if batch["processing_status"] != "ended":
                return None
            content = (await self._request("GET", "messages/batches/" + batch_id + "/results")).text
            result = {}
            for line in content.splitlines():
                row = json.loads(line)
                if row["custom_id"] in result:
                    raise ValueError("Duplicate provider custom ID")
                value = row["result"]
                result[row["custom_id"]] = (
                    value["message"] if value["type"] == "succeeded" else {"error": value}
                )
        else:
            batch = (await self._request("GET", batch_id)).json()
            if not batch.get("done"):
                return None
            response = batch.get("response", {})
            outputs = response.get("inlinedResponses", [])
            if isinstance(outputs, dict):
                outputs = outputs.get("inlinedResponses", [])
            result = {}
            for item in outputs:
                key = item["metadata"]["key"]
                if key in result:
                    raise ValueError("Duplicate provider custom ID")
                result[key] = item.get("response") or {"error": item.get("error", item)}
        if set(result) - set(keys):
            raise ValueError("Unexpected provider custom ID")
        # Terminal partial batches terminate every waiter; missing usage stays reserved.
        return {key: result.get(key, {"error": {"type": "terminal_missing_member"}}) for key in keys}
