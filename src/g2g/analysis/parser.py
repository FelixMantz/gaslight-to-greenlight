from __future__ import annotations

import re
from dataclasses import dataclass

RESERVED = re.compile(r"</?\s*(?:decision|explanation|recommendation|suspicion_score)\b", re.IGNORECASE)
ENVELOPE = re.compile(
    r"\A\s*<decision>\s*<explanation>(.*?)</explanation>\s*"
    r"<recommendation>(.*?)</recommendation>\s*"
    r"<suspicion_score>(.*?)</suspicion_score>\s*</decision>\s*\Z",
    re.DOTALL,
)
SCORE = re.compile(r"(?:100|[0-9]{1,2})\Z", re.ASCII)


@dataclass(frozen=True)
class Decision:
    status: str
    explanation: str = ""
    recommendation: str = "escalate"
    score: int | None = None
    reason: str | None = None


def parse_decision(text: str, provider_status: str = "success", max_bytes: int = 65536) -> Decision:
    if provider_status in {"refused", "truncated", "provider_error"}:
        return Decision(provider_status, reason=provider_status)
    if len(text.encode("utf-8")) > max_bytes:
        return Decision("invalid", reason="output_size")
    match = ENVELOPE.fullmatch(text)
    if not match:
        return Decision("invalid", reason="delimiter_structure")
    explanation, recommendation, score = (x.strip() for x in match.groups())
    if any(RESERVED.search(x) for x in match.groups()):
        return Decision("invalid", reason="reserved_tag_in_body")
    if not explanation:
        return Decision("invalid", reason="empty_explanation")
    recommendation = recommendation.lower()
    if recommendation not in {"allow", "block", "escalate"}:
        return Decision("invalid", reason="recommendation")
    if not SCORE.fullmatch(score):
        return Decision("invalid", reason="integer_score")
    return Decision("valid", explanation, recommendation, int(score))


def apply_threshold(decision: Decision, locked_record: dict) -> dict:
    if locked_record.get("comparator") != ">=":
        raise ValueError("Unsupported threshold comparator")
    if decision.status != "valid":
        return {"operative": "escalate", "flag": None, "flag_or_escalate": True, "reason": decision.reason}
    flag = decision.score >= locked_record["tau"]
    operative = "block" if flag else "allow"
    return {
        "operative": operative,
        "flag": flag,
        "flag_or_escalate": flag,
        "comparison": f"{decision.score} >= {locked_record['tau']}",
        "recommendation_disagreement": decision.recommendation != operative,
    }
