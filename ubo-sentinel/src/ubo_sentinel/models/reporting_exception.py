from typing import Literal

from pydantic import BaseModel, ConfigDict

from ubo_sentinel.models.provenance import NonEmptyStr, Provenance

# `PSC_STATEMENT`: a Companies House statement that a person with significant
# control is not identified or not confirmed, or is super-secure (Step 17).
RepexCategory = Literal["DIRECT_PARENT", "ULTIMATE_PARENT", "PSC_STATEMENT"]


class ReportingException(BaseModel):
    """Why an entity does not report a parent. A data-gap signal, not an edge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_id: NonEmptyStr
    category: RepexCategory
    reason: NonEmptyStr
    provenance: Provenance
