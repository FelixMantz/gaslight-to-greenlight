from __future__ import annotations

import copy


class NativeCodec:
    """Full native-prefix preservation. Never reconstruct opaque state from text."""

    def __init__(self, provider: str, settings: dict):
        if provider not in {"fixture", "openai", "anthropic", "google", "together"}:
            raise ValueError("Unsupported provider")
        self.provider, self.settings = provider, copy.deepcopy(settings)

    def encode(self, messages: list[dict], native_prefix: dict | None = None) -> dict:
        p = self.provider
        if native_prefix is not None:
            request = copy.deepcopy(native_prefix)
            if request["model"] != self.settings["model"]:
                raise ValueError("Cross-model native replay forbidden")
            # Native prefix includes frozen settings and system. Only append new current input.
            current = messages[-1]
            if p == "google":
                request["contents"].append({"role": "user", "parts": [{"text": current["content"]}]})
            elif p == "openai":
                request["input"].append(copy.deepcopy(current))
            else:
                request["messages"].append(copy.deepcopy(current))
            return request
        cap = self.settings.get("max_output_tokens", 4096)
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        turns = [copy.deepcopy(m) for m in messages if m["role"] != "system"]
        if p == "openai":
            return {
                "model": self.settings["model"],
                "instructions": system,
                "input": turns,
                "max_output_tokens": cap,
                "store": False,
                "include": ["reasoning.encrypted_content"],
                "reasoning": {"effort": self.settings.get("effort", "medium")},
            }
        if p == "anthropic":
            return {
                "model": self.settings["model"],
                "system": system,
                "messages": turns,
                "max_tokens": cap,
                "thinking": {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "error"}},
                "output_config": {"effort": self.settings.get("effort", "medium")},
            }
        if p == "google":
            return {
                "model": self.settings["model"],
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [
                    {
                        "role": "model" if m["role"] == "assistant" else "user",
                        "parts": [{"text": m["content"]}],
                    }
                    for m in turns
                ],
                "generationConfig": {"maxOutputTokens": cap, "thinkingConfig": {"includeThoughts": True}},
            }
        return {
            "model": self.settings["model"],
            "messages": copy.deepcopy(messages),
            "max_tokens": cap,
            **({"stream": False} if p == "together" else {}),
        }

    def advance_history(self, request: dict, raw: dict) -> dict:
        result = copy.deepcopy(request)
        if self.provider == "openai":
            result["input"] += copy.deepcopy(raw["output"])
        elif self.provider == "anthropic":
            self.check_transformations(raw)
            result["messages"].append({"role": "assistant", "content": copy.deepcopy(raw["content"])})
        elif self.provider == "google":
            result["contents"].append(copy.deepcopy(raw["candidates"][0]["content"]))
        elif self.provider == "together":
            result["messages"].append(copy.deepcopy(raw["choices"][0]["message"]))
        else:
            result["messages"].append({"role": "assistant", "content": raw["text"]})
        return result

    @staticmethod
    def check_transformations(raw):
        if "input_transformations" not in raw:
            raise ValueError("Required Anthropic transformation metadata missing")
        if raw["input_transformations"]:
            raise ValueError("Replay transformation requires review; no silent thinking drop")

    def decode(self, raw: dict) -> tuple[str, str, dict]:
        p = self.provider
        if raw.get("error"):
            return "", "provider_error", {}
        if p == "fixture":
            return raw["text"], raw.get("status", "success"), raw.get("usage", {})
        if p == "openai":
            blocks = [
                c for o in raw.get("output", []) if o.get("type") == "message" for c in o.get("content", [])
            ]
            text = "".join(c.get("text", "") for c in blocks if c.get("type") == "output_text")
            status = (
                "refused"
                if any(c.get("type") == "refusal" for c in blocks)
                else "truncated"
                if raw.get("status") == "incomplete"
                else "provider_error"
                if raw.get("status") not in ("completed", None)
                else "success"
            )
            u = raw.get("usage", {})
            return (
                text,
                status,
                {
                    "input_tokens": u.get("input_tokens"),
                    "output_tokens": u.get("output_tokens"),
                    "cache_read_tokens": u.get("input_tokens_details", {}).get("cached_tokens", 0),
                    "reasoning_tokens_in_output": u.get("output_tokens_details", {}).get(
                        "reasoning_tokens", 0
                    ),
                },
            )
        if p == "anthropic":
            self.check_transformations(raw)
            content = raw.get("content", [])
            status = (
                "truncated"
                if raw.get("stop_reason") == "max_tokens"
                else "refused"
                if raw.get("stop_reason") == "refusal"
                else "success"
            )
            u = raw.get("usage", {})
            # Anthropic reports uncached input separately from cache creation/read.
            return (
                "".join(c.get("text", "") for c in content if c["type"] == "text"),
                status,
                {
                    "input_tokens": None
                    if u.get("input_tokens") is None
                    else u["input_tokens"]
                    + u.get("cache_read_input_tokens", 0)
                    + u.get("cache_creation_input_tokens", 0),
                    "output_tokens": u.get("output_tokens"),
                    "cache_read_tokens": u.get("cache_read_input_tokens", 0),
                    "cache_write_tokens": u.get("cache_creation_input_tokens", 0),
                },
            )
        if p == "google":
            candidates = raw.get("candidates", [])
            if not candidates:
                return (
                    "",
                    "refused" if raw.get("promptFeedback", {}).get("blockReason") else "provider_error",
                    {},
                )
            candidate = candidates[0]
            text = "".join(
                part.get("text", "")
                for part in candidate.get("content", {}).get("parts", [])
                if not part.get("thought")
            )
            reason = candidate.get("finishReason")
            status = "truncated" if reason == "MAX_TOKENS" else "success" if reason == "STOP" else "refused"
            u = raw.get("usageMetadata", {})
            return (
                text,
                status,
                {
                    "input_tokens": u.get("promptTokenCount"),
                    "output_tokens": None
                    if "candidatesTokenCount" not in u
                    else u["candidatesTokenCount"] + u.get("thoughtsTokenCount", 0),
                    "cache_read_tokens": u.get("cachedContentTokenCount", 0),
                    "reasoning_tokens_in_output": u.get("thoughtsTokenCount", 0),
                },
            )
        choice = raw.get("choices", [{}])[0]
        reason = choice.get("finish_reason")
        msg = choice.get("message", {})
        status = "truncated" if reason == "length" else "refused" if msg.get("refusal") else "success"
        u = raw.get("usage", {})
        return (
            msg.get("content") or "",
            status,
            {"input_tokens": u.get("prompt_tokens"), "output_tokens": u.get("completion_tokens")},
        )
