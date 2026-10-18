from __future__ import annotations

import copy
import json

from g2g.compiler import SYSTEM
from g2g.util import digest, write_json


def probe_replay_structure(codec, request: dict, response: dict) -> dict:
    prefix = codec.advance_history(request, response)
    reloaded = json.loads(json.dumps(prefix, ensure_ascii=False))
    if prefix != reloaded:
        raise ValueError("Native state did not round-trip")
    continuation = codec.encode([{"role": "user", "content": "Continue the fictional task."}], reloaded)
    field = (
        "input" if codec.provider == "openai" else "contents" if codec.provider == "google" else "messages"
    )
    if continuation[field][:-1] != prefix[field]:
        raise ValueError("Branch changed earlier native content")
    mutated = copy.deepcopy(prefix)
    mutated[field][0]["g2g_negative_control"] = True
    return {
        "structural_roundtrip": True,
        "prefix_hash": digest(prefix),
        "mutated_prefix_hash": digest(mutated),
        "server_replay_verified": False,
        "binding_negative_control_verified": False,
        "passed": False,
        "note": "Offline structure alone cannot pass a paid replay capability gate",
    }


async def probe_replay(engine):
    """Four actual replies, reload/branch, then a separate deliberate prefix mutation.

    All calls use the journal and cap checks. Positive/negative payload results are
    archived; reviewer must assess provider transformation metadata and distribution
    checks before signing a gate. An HTTP 200 is never automatically accepted.
    """
    native, messages, dependency = None, [], []
    prefix = None
    for turn in range(4):
        current = {
            "role": "user",
            "content": f"Fictional task {turn + 1}: list the next integer after {turn + 1}.",
        }
        spec = engine._spec(
            "probe",
            str(turn),
            "turn",
            f"probe-{turn}",
            "probe",
            messages=[{"role": "system", "content": SYSTEM}] + messages + [current],
            native_prefix=native,
            dependencies=dependency,
            metadata={"probe": "positive", "turn": turn + 1},
        )
        result = (await engine.wave(f"probe-{turn}", [spec]))[spec["key"]]
        request = json.loads(engine.journal.get(spec["key"])["request_json"])["payload"]
        native = engine.codec.advance_history(request, result["raw"])
        messages += [current, {"role": "assistant", "content": result["final_text"]}]
        dependency = [spec["key"]]
    prefix = json.loads(json.dumps(native))
    outputs = {}
    for mode in ("positive", "negative"):
        replay = copy.deepcopy(prefix)
        if mode == "negative":
            field = (
                "input"
                if engine.codec.provider == "openai"
                else "contents"
                if engine.codec.provider == "google"
                else "messages"
            )
            replay[field][0]["content" if field != "contents" else "parts"] = (
                "Deliberately changed earlier user content."
                if field != "contents"
                else [{"text": "Deliberate prefix mutation"}]
            )
        spec = engine._spec(
            "probe",
            mode,
            "branch",
            f"probe-{mode}",
            "probe",
            messages=messages + [{"role": "user", "content": "Continue."}],
            native_prefix=replay,
            dependencies=dependency,
            metadata={"probe": mode, "prefix_hash": digest(replay)},
        )
        outputs[mode] = (await engine.wave(f"probe-branch-{mode}", [spec]))[spec["key"]]
    result = {
        "passed": False,
        "config_hash": digest(engine.settings),
        "prefix_hash": digest(prefix),
        "observations": outputs,
        "requires_review": "Check binding error, transformation metadata, endpoint/state semantics",
    }
    write_json(engine.stage / "probe-review.json", result, immutable=True)
    return result
