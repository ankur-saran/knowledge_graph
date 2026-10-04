from typing import Literal

from pydantic import BaseModel, ConfigDict

from ubo_sentinel.models.provenance import NonEmptyStr, Provenance

RepexCategory = Literal["DIRECT_PARENT", "ULTIMATE_PARENT"]


class ReportingException(BaseModel):
    """Why an entity does not report a parent. A data-gap signal, not an edge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_id: NonEmptyStr
    category: RepexCategory
    reason: NonEmptyStr
    provenance: Provenance
