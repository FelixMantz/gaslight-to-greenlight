from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Evidence(Contract):
    id: str
    source_role: Literal["operator", "record", "actor"]
    text: str


class VisiblePacket(Contract):
    actor_id: str
    current_time: datetime
    policy: str = Field(min_length=1)
    evidence: list[Evidence]
    proposed_action: str = Field(min_length=1)
    actor_claim: str | None = None

    @field_validator("current_time")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Timezone required")
        return value

    @model_validator(mode="after")
    def unique_evidence(self):
        if len({e.id for e in self.evidence}) != len(self.evidence):
            raise ValueError("Duplicate evidence IDs")
        return self


class AuditCase(Contract):
    case_id: str
    family_id: str
    split: Literal["pilot", "calibration", "main", "state"]
    domain: Literal["disclosure", "action_authorisation", "reporting_integrity"]
    source: dict[str, Any]
    visible: VisiblePacket
    upstream_task_id: str | None = None


class GoldRecord(Contract):
    case_id: str
    label: Literal["benign", "violating", "authorised", "prohibited"]
    rule_ids: list[str] = []
    evidence_ids: list[str] = []
    event_ids: list[str] = []
    rationale: str
    ambiguities: list[str] = []
    review_status: Literal["unreviewed", "accepted", "excluded", "adjudicated"] = "unreviewed"


class IdentityBinding(Contract):
    benefactor_id: str
    unrelated_actor_id: str

    @model_validator(mode="after")
    def distinct(self):
        if self.benefactor_id == self.unrelated_actor_id:
            raise ValueError("Actor identity collision")
        for name in (self.benefactor_id, self.unrelated_actor_id):
            if not name.strip() or any(c in name for c in "{}<>\n"):
                raise ValueError("Invalid actor name")
        return self


class History(Contract):
    history_id: str
    model_id: str
    script_id: str
    pair_id: str
    condition: Literal["neutral", "helpful", "state"]
    binding: IdentityBinding
    native_payload: dict
    readable_messages: list[dict]
    prefix_hash: str
    turn: int | None = None
    dependencies: list[str] = []
    grant_template_id: str | None = None
    base_prefix_id: str | None = None


class Request(Contract):
    key: str
    study_id: str
    phase: str
    model_id: str
    purpose: Literal["decision", "history", "extraction", "generation", "probe"]
    block_id: str
    payload: dict
    config_hash: str
    planning_microusd: int = Field(ge=0)
    maximum_microusd: int = Field(ge=0)
    metadata: dict = {}
    dependencies: list[str] = []
    attempt: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def bounded(self):
        if self.planning_microusd > self.maximum_microusd:
            raise ValueError("Planning allowance exceeds finite API cap")
        return self


class Observation(Contract):
    observation_id: str
    key: str
    status: Literal["valid", "invalid", "refused", "truncated", "provider_error", "success"]
    final_text: str
    raw: dict
    usage: dict
    actual_microusd: int | None
    price_version: str
    batch_id: str
    collected_at: str
    archive: str
    archive_sha256: str


class BranchEvent(Contract):
    study_id: str
    key: str | None
    attempt: int
    time: str
    function: str
    source_commit: str
    branch: str
    detail: dict = {}
