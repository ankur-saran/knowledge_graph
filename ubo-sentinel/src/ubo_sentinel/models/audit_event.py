from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ubo_sentinel.models.decision import UtcDatetime
from ubo_sentinel.models.ontology import Role
from ubo_sentinel.models.provenance import NonEmptyStr

# One event per state transition (the decision statuses), plus a screen that
# failed and so made no decision. Later steps that add other event kinds widen
# this alias here.
EventType = Literal["RECOMMENDED", "APPROVED", "OVERRIDDEN", "ESCALATED", "SCREEN_FAILED"]


class AuditEvent(BaseModel):
    """One line of the append-only, hash-chained audit log."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=0)
    decision_id: NonEmptyStr
    event_type: EventType
    actor: NonEmptyStr
    role: Role
    note: str | None = None
    payload: dict[str, Any] = {}
    created_at: UtcDatetime
    prev_hash: NonEmptyStr
    hash: NonEmptyStr
