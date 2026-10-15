from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from ubo_sentinel.models.provenance import NonEmptyStr, Provenance, sort_provenance

# The ontology calls these node labels.
EntityType = Literal["LegalEntity", "Person"]
# The legal status of the entity: does it exist?
EntityStatus = Literal["ACTIVE", "INACTIVE", "UNKNOWN"]
# The status of its LEI record (GLEIF's registration status): is what the
# record says kept up to date? An entity may be active while its LEI has lapsed.
LeiStatus = Literal[
    "ISSUED",
    "LAPSED",
    "RETIRED",
    "MERGED",
    "DUPLICATE",
    "ANNULLED",
    "CANCELLED",
    "TRANSFERRED",
    "PENDING_TRANSFER",
    "PENDING_ARCHIVAL",
    "PENDING_VALIDATION",
]


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
    lei_status: LeiStatus | None = None
    entity_type: EntityType
    # Summary only; the full records are `ReportingException`.
    repex_reason: str | None = None
    provenance: Provenance
    supporting_records: list[Provenance] = []

    @field_validator("aliases")
    @classmethod
    def _sort_aliases(cls, value: list[str]) -> list[str]:
        return sorted(set(value))

    @model_validator(mode="after")
    def _lei_status_needs_an_lei(self) -> Self:
        if self.lei_status is not None and self.lei is None:
            raise ValueError("an LEI status needs an LEI")
        return self

    @field_validator("supporting_records")
    @classmethod
    def _sort_supporting_records(cls, value: list[Provenance]) -> list[Provenance]:
        return sort_provenance(value)
