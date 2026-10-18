import copy

import pytest

from g2g.adapters.codecs import NativeCodec
from g2g.budget import price_usage
from g2g.probes import probe_replay_structure


def test_openai_opaque_order_and_reasoning_billing():
    c = NativeCodec("openai", {"model": "pinned", "max_output_tokens": 4096})
    req = c.encode([{"role": "system", "content": "fixed"}, {"role": "user", "content": "hello"}])
    raw = {
        "status": "completed",
        "output": [
            {"type": "reasoning", "id": "r", "encrypted_content": "opaque=="},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "final"}]},
        ],
        "usage": {
            "input_tokens": 20,
            "output_tokens": 100,
            "output_tokens_details": {"reasoning_tokens": 90},
        },
    }
    prefix = c.advance_history(req, raw)
    before = copy.deepcopy(prefix)
    next_request = c.encode([{"role": "user", "content": "next"}], prefix)
    assert prefix == before and next_request["input"][1:3] == raw["output"]
    text, status, usage = c.decode(raw)
    assert text == "final" and usage["output_tokens"] == 100
    assert price_usage(usage, {"input": 1, "output": 2}) == 220
    assert not probe_replay_structure(c, req, raw)["passed"]


def test_anthropic_signature_binding_and_account_metadata():
    c = NativeCodec("anthropic", {"model": "pinned", "max_output_tokens": 4096})
    req = c.encode([{"role": "system", "content": "fixed"}, {"role": "user", "content": "hello"}])
    assert req["thinking"]["type"] == "adaptive" and "budget_tokens" not in req["thinking"]
    raw = {
        "content": [
            {"type": "thinking", "thinking": "internal", "signature": "s"},
            {"type": "text", "text": "final"},
        ],
        "input_transformations": [],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": 3,
        },
    }
    assert c.advance_history(req, raw)["messages"][-1]["content"] == raw["content"]
    assert c.decode(raw)[2]["input_tokens"] == 18
    raw["input_transformations"] = [{"type": "thinking_dropped"}]
    with pytest.raises(ValueError):
        c.advance_history(req, raw)


def test_google_parts_signatures_and_final_only():
    c = NativeCodec("google", {"model": "pinned"})
    req = c.encode([{"role": "system", "content": "fixed"}, {"role": "user", "content": "hello"}])
    raw = {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "role": "model",
                    "parts": [
                        {"thought": True, "text": "reason", "thoughtSignature": "opaque"},
                        {"text": "final"},
                    ],
                },
            }
        ],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 20, "thoughtsTokenCount": 30},
    }
    assert c.advance_history(req, raw)["contents"][-1] == raw["candidates"][0]["content"]
    assert c.decode(raw)[0] == "final" and c.decode(raw)[2]["output_tokens"] == 50
