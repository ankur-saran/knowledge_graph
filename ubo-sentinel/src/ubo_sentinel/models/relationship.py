from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from ubo_sentinel.models.canonical import quantize, short_id
from ubo_sentinel.models.provenance import NonEmptyStr, Provenance, sort_provenance

# Direction: OWNS is owner -> asset, CONSOLIDATED_BY is child -> parent,
# CONTROLS is controller -> controlled.
RelType = Literal["OWNS", "CONSOLIDATED_BY", "CONTROLS"]
Basis = Literal["shareholding", "accounting_consolidation", "directorship", "other"]

# Ownership percentage: Decimal, never float, so sums do not depend on input order.
Pct = Annotated[Decimal, AfterValidator(quantize), Field(gt=0, le=100)]


def relationship_id(subject_id: str, object_id: str, rel_type: str, is_ultimate: bool) -> str:
    return short_id([subject_id, object_id, rel_type, is_ultimate])


class Relationship(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: NonEmptyStr
    subject_id: NonEmptyStr
    object_id: NonEmptyStr
    rel_type: RelType
    pct: Pct | None = None
    basis: Basis
    is_ultimate: bool = False
    provenance: Provenance
    supporting_records: list[Provenance] = []

    @field_validator("supporting_records")
    @classmethod
    def _sort_supporting_records(cls, value: list[Provenance]) -> list[Provenance]:
        return sort_provenance(value)
