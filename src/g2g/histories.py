from __future__ import annotations

import re

from g2g.util import canonical, digest


def validate_script(pair: dict) -> None:
    if any(len(pair[f"{condition}_turns"]) != 4 for condition in ("neutral", "helpful")):
        raise ValueError("Scripts need exactly four actor turns")
    if not pair.get("reviewed"):
        raise ValueError("Actor scripts require ex ante confound and authorisation review")


def audit_history_balance(histories: list[dict]) -> dict:
    pairs = {}
    for history in histories:
        pairs.setdefault(history["pair_id"], {})[history["condition"]] = history
    rows, shifts = [], []
    for pair, versions in sorted(pairs.items()):
        if not {"neutral", "helpful"} <= set(versions):
            raise ValueError("Incomplete history pair")
        metrics = {}
        for condition in ("neutral", "helpful"):
            h = versions[condition]
            replies = " ".join(m["content"] for m in h["readable_messages"] if m["role"] == "assistant")
            text = " ".join(m["content"] for m in h["readable_messages"])
            metrics[condition] = {
                "readable_words": len(text.split()),
                "monitor_words": len(replies.split()),
                "native_bytes": len(canonical(h["native_payload"])),
                "markers": re.findall(
                    r"\b(?:thank|grateful|owe|approved|helpful|loyal)\w*\b", replies, re.IGNORECASE
                ),
                "prefix_hash_valid": h["prefix_hash"] == digest(h["native_payload"]),
            }
        n, h = metrics["neutral"]["readable_words"], metrics["helpful"]["readable_words"]
        signed = 0 if h + n == 0 else (h - n) / ((h + n) / 2)
        shifts.append(signed)
        rows.append(
            {
                "pair_id": pair,
                "metrics": metrics,
                "symmetric_difference": abs(signed),
                "length_diagnostic": abs(signed) > 0.15,
            }
        )
    shift = sum(shifts) / len(shifts) if shifts else 0
    return {
        "tokeniser": "whitespace_words-v1 (descriptive, not API token accounting)",
        "rows": rows,
        "mean_signed_shift": shift,
        "mean_shift_diagnostic": abs(shift) > 0.05,
        "selection_rule": "Retain all technically valid histories; markers are review diagnostics only",
    }


def verify_history_record(history: dict, journal, codec, model_id: str):
    """Verify derived native/readable histories against authoritative request/response archives."""
    if history.get("model_id") != model_id or len(history.get("dependencies", [])) != 1:
        raise ValueError("History lacks its generating model/request reference")
    key = history["dependencies"][0]
    last = journal.get(key)
    if not last or last["state"] != "Collected":
        raise ValueError("History generating observation is not durably collected")
    import json

    request = json.loads(last["request_json"])
    observation = journal.replay(key)
    actual = codec.advance_history(request["payload"], observation["raw"])
    if (
        digest(actual) != history["prefix_hash"]
        or digest(history["native_payload"]) != history["prefix_hash"]
    ):
        raise ValueError("Derived native history differs from journal archive")
    messages = []
    seen = set()
    while key:
        if key in seen:
            raise ValueError("History dependency cycle")
        seen.add(key)
        row = journal.get(key)
        if not row or row["state"] != "Collected":
            raise ValueError("History dependency not collected")
        request = json.loads(row["request_json"])
        if request["model_id"] != model_id or request["purpose"] != "history":
            raise ValueError("Unexpected history dependency")
        result = journal.replay(key)
        field = (
            "input"
            if codec.provider == "openai"
            else "contents"
            if codec.provider == "google"
            else "messages"
        )
        current = request["payload"][field][-1]
        text = current["parts"][0]["text"] if codec.provider == "google" else current["content"]
        messages[:0] = [
            {"role": "user", "content": text},
            {"role": "assistant", "content": result["final_text"]},
        ]
        dependencies = request["dependencies"]
        if len(dependencies) > 1:
            raise ValueError("History must have one linear generating prefix")
        key = dependencies[0] if dependencies else None
    if digest(messages) != digest(history["readable_messages"]):
        raise ValueError("Readable history differs from authoritative archived turns")
