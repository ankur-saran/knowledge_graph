from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from ubo_sentinel.models.canonical import quantize, short_id
from ubo_sentinel.models.provenance import NonEmptyStr, Provenance, sort_provenance

# Direction: OWNS is owner -> asset, CONSOLIDATED_BY is child -> parent,
# CONTROLS is controller -> controlled.
RelType = Literal["OWNS", "CONSOLIDATED_BY", "CONTROLS"]
Basis = Literal["shareholding", "accounting_consolidation", "directorship", "other"]
# What a stake is a stake of (Step 16): shares, or votes.
Interest = Literal["equity", "voting"]
# Which ends of a stated range belong to it: "(]" is "more than low, at most high".
PctBounds = Literal["[]", "(]", "[)", "()"]
PCT_BOUNDS: tuple[PctBounds, ...] = ("[]", "(]", "[)", "()")

# Ownership percentage: Decimal, never float, so sums do not depend on input order.
Pct = Annotated[Decimal, AfterValidator(quantize), Field(gt=0, le=100)]
# The low end of a range may be 0 ("less than 10 %" is `(0, 10)`).
PctLow = Annotated[Decimal, AfterValidator(quantize), Field(ge=0, le=100)]


def relationship_id(
    subject_id: str,
    object_id: str,
    rel_type: str,
    is_ultimate: bool,
    interest: str | None = "equity",
) -> str:
    """An edge's id. A voting stake is another edge than the equity stake of the same pair;
    an equity edge keeps the id it had before stakes had an interest."""
    if interest in (None, "equity"):
        return short_id([subject_id, object_id, rel_type, is_ultimate])
    return short_id([subject_id, object_id, rel_type, is_ultimate, interest])


class Relationship(BaseModel):
    """An edge, and the stake it states.

    `pct` is an exact stake. A stated range has `pct` null and its ends in
    `pct_low`, `pct_high` and `pct_bounds`; a point has all four, equal and
    closed (`[]`); a stake of unknown size has none. Given only `pct`, the
    other three are filled in; given a closed range of width 0, `pct` is.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: NonEmptyStr
    subject_id: NonEmptyStr
    object_id: NonEmptyStr
    rel_type: RelType
    pct: Pct | None = None
    pct_low: PctLow | None = None
    pct_high: Pct | None = None
    pct_bounds: PctBounds | None = None
    interest: Interest = "equity"
    basis: Basis
    is_ultimate: bool = False
    provenance: Provenance
    supporting_records: list[Provenance] = []

    @model_validator(mode="before")
    @classmethod
    def _fill_point(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        pct = data.get("pct")
        low, high, bounds = data.get("pct_low"), data.get("pct_high"), data.get("pct_bounds")
        if pct is not None and low is None and high is None and bounds is None:
            return {**data, "pct_low": pct, "pct_high": pct, "pct_bounds": "[]"}
        if (
            pct is None
            and low is not None
            and high is not None
            and bounds == "[]"
            and Decimal(str(low)) == Decimal(str(high))
        ):
            return {**data, "pct": high}
        return data

    @model_validator(mode="after")
    def _check_stake(self) -> Self:
        ends = (self.pct_low, self.pct_high, self.pct_bounds)
        if any(end is None for end in ends) != all(end is None for end in ends):
            raise ValueError("a range states its low end, its high end and its bounds together")
        if self.pct_low is None:
            if self.pct is not None:
                raise ValueError("an exact stake is also its range")
            return self
        if self.pct_low > self.pct_high:
            raise ValueError("pct_low may not be above pct_high")
        if self.pct_low == self.pct_high:
            if self.pct_bounds != "[]":
                raise ValueError("a range of one value is closed: '[]'")
            if self.pct != self.pct_high:
                raise ValueError("a range of one value is the exact stake `pct`")
        elif self.pct is not None:
            raise ValueError("an exact stake is a range of one value")
        return self

    @property
    def stated(self) -> bool:
        """True when the edge states a stake: a point or a range."""
        return self.pct_high is not None

    @property
    def is_range(self) -> bool:
        return self.pct_high is not None and self.pct is None

    @field_validator("supporting_records")
    @classmethod
    def _sort_supporting_records(cls, value: list[Provenance]) -> list[Provenance]:
        return sort_provenance(value)
