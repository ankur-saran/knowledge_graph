from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from ubo_sentinel.models.provenance import NonEmptyStr, Provenance, sort_provenance

# The ontology calls these node labels.
EntityType = Literal["LegalEntity", "Person"]
EntityStatus = Literal["ACTIVE", "INACTIVE", "LAPSED", "RETIRED", "UNKNOWN"]


def entity_id(lei: str | None, source: str, source_record_id: str) -> str:
    """`lei:<LEI>` when an LEI exists, otherwise `<source>:<source_record_id>`."""
    return f"lei:{lei}" if lei else f"{source}:{source_record_id}"


class Entity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: NonEmptyStr
    lei: str | None = None
    registration_authority_id: str | None = None
    registration_number: str | None = None
    legal_name: NonEmptyStr
    aliases: list[str] = []
    jurisdiction: str | None = None
    status: EntityStatus
    entity_type: EntityType
    # Summary only; the full records are `ReportingException`.
    repex_reason: str | None = None
    provenance: Provenance
    supporting_records: list[Provenance] = []

    @field_validator("aliases")
    @classmethod
    def _sort_aliases(cls, value: list[str]) -> list[str]:
        return sorted(set(value))

    @field_validator("supporting_records")
    @classmethod
    def _sort_supporting_records(cls, value: list[Provenance]) -> list[Provenance]:
        return sort_provenance(value)
