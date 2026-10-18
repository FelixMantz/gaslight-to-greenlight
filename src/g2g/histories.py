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
