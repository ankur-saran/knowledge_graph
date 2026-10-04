from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

NonEmptyStr = Annotated[str, Field(min_length=1)]
Confidence = Annotated[float, Field(ge=0, le=1)]


class Provenance(BaseModel):
    """Where a fact came from. Carried by every node, edge and sanction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: NonEmptyStr
    source_record_id: NonEmptyStr
    snapshot_id: NonEmptyStr
    as_of: date
    confidence: Confidence

    def sort_key(self) -> tuple[str, str, str, str]:
        return (self.source, self.source_record_id, self.snapshot_id, self.as_of.isoformat())


def sort_provenance(records: list[Provenance]) -> list[Provenance]:
    return sorted(records, key=Provenance.sort_key)
