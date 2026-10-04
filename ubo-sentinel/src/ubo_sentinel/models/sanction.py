from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ubo_sentinel.models.canonical import short_id
from ubo_sentinel.models.provenance import Confidence, NonEmptyStr, Provenance

MatchType = Literal["DIRECT", "LEI", "REGISTRATION", "FUZZY_NAME"]

# The fields that describe the designation itself. A snapshot diff compares only
# these; `match_type` and `match_confidence` describe the link to `entity_id`.
DESIGNATION_FIELDS = ("program", "list_date", "list_source", "is_active")


def sanction_id(source: str, source_record_id: str, entity_id: str) -> str:
    return short_id([source, source_record_id, entity_id])


class Sanction(BaseModel):
    """One row per (designation, entity it reaches)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: NonEmptyStr
    entity_id: NonEmptyStr
    program: NonEmptyStr
    list_date: date | None = None
    list_source: NonEmptyStr
    is_active: bool
    match_type: MatchType
    match_confidence: Confidence
    provenance: Provenance
