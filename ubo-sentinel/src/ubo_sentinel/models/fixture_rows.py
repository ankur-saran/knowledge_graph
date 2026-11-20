"""Row schemas for the fixture CSVs, and a reader.

The CSVs are source-shaped: they hold what a source publishes, not canonical
fields. Ids, `repex_reason`, `match_type` and `match_confidence` are derived later.
"""

import csv
from datetime import date
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ubo_sentinel.models.entity import EntityStatus, EntityType, LeiStatus, entity_id
from ubo_sentinel.models.provenance import Confidence, NonEmptyStr
from ubo_sentinel.models.relationship import Basis, Interest, Pct, PctBounds, PctLow, RelType
from ubo_sentinel.models.reporting_exception import RepexCategory

ALIAS_SEPARATOR = "|"

Lei = Annotated[str, Field(pattern=r"^[A-Z0-9]{18}[0-9]{2}$")]

_BASIS_BY_REL_TYPE: dict[str, set[str]] = {
    "OWNS": {"shareholding"},
    "CONSOLIDATED_BY": {"accounting_consolidation"},
    "CONTROLS": {"directorship", "other"},
}


class _Row(BaseModel):
    """Columns every fixture file has; they become the row's `Provenance`."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    # Columns a file may leave out, all of them or none: read as empty when it does.
    # Added after files were written, so the files keep their bytes (Step 16).
    OPTIONAL_COLUMNS: ClassVar[tuple[str, ...]] = ()

    source: NonEmptyStr
    source_record_id: NonEmptyStr
    as_of: date
    confidence: Confidence

    @property
    def ref(self) -> str:
        """How other rows point at this one: `<source>:<source_record_id>`."""
        return f"{self.source}:{self.source_record_id}"


class EntityRow(_Row):
    lei: Lei | None = None
    registration_authority_id: NonEmptyStr | None = None
    registration_number: NonEmptyStr | None = None
    legal_name: NonEmptyStr
    aliases: list[NonEmptyStr] = []
    jurisdiction: NonEmptyStr | None = None
    status: EntityStatus
    lei_status: LeiStatus | None = None
    entity_type: EntityType

    @model_validator(mode="after")
    def _lei_status_needs_an_lei(self) -> Self:
        if self.lei_status is not None and self.lei is None:
            raise ValueError("an LEI status needs an LEI")
        return self

    @field_validator("aliases", mode="before")
    @classmethod
    def _split_aliases(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str):
            return value.split(ALIAS_SEPARATOR)
        return value

    def canonical_id(self) -> str:
        """The id of this record alone, before records of one entity are merged."""
        return entity_id(self.lei, self.source, self.source_record_id)


class RelationshipRow(_Row):
    OPTIONAL_COLUMNS: ClassVar[tuple[str, ...]] = ("pct_low", "pct_high", "pct_bounds", "interest")

    subject_ref: NonEmptyStr
    object_ref: NonEmptyStr
    rel_type: RelType
    # Empty means the source gives no percentage; it never means 0.
    pct: Pct | None = None
    basis: Basis
    is_ultimate: bool
    # A stake stated as a range (Step 16): its ends and which of them belong to it.
    # Empty with `pct` stated: the point `pct`. `interest` empty is `equity`.
    pct_low: PctLow | None = None
    pct_high: Pct | None = None
    pct_bounds: PctBounds | None = None
    interest: Interest | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.subject_ref == self.object_ref:
            raise ValueError("an edge may not join an entity to itself")
        if self.basis not in _BASIS_BY_REL_TYPE[self.rel_type]:
            raise ValueError(f"basis '{self.basis}' does not fit {self.rel_type}")
        if self.is_ultimate and self.rel_type != "CONSOLIDATED_BY":
            raise ValueError("only a CONSOLIDATED_BY edge may be ultimate")
        ends = (self.pct_low, self.pct_high, self.pct_bounds)
        stated = self.pct is not None or self.pct_high is not None
        if stated and self.rel_type == "CONTROLS":
            raise ValueError("a CONTROLS edge has no percentage")
        if self.interest not in (None, "equity") and self.rel_type == "CONTROLS":
            raise ValueError("a CONTROLS edge has no interest")
        if any(end is None for end in ends) != all(end is None for end in ends):
            raise ValueError("a range states pct_low, pct_high and pct_bounds together")
        if self.pct_low is not None:
            assert self.pct_high is not None
            if self.pct_low > self.pct_high:
                raise ValueError("pct_low may not be above pct_high")
            point = self.pct_low == self.pct_high
            if point and self.pct_bounds != "[]":
                raise ValueError("a range of one value is closed: '[]'")
            if self.pct is not None and not (point and self.pct == self.pct_high):
                raise ValueError("pct and a range of other values")
        return self


class SanctionRow(_Row):
    entity_ref: NonEmptyStr
    program: NonEmptyStr
    list_date: date | None = None
    list_source: NonEmptyStr
    is_active: bool


class RepexRow(_Row):
    entity_ref: NonEmptyStr
    category: RepexCategory
    reason: NonEmptyStr


class AddressRow(_Row):
    """An address of an entity (Step 14): context for an investigation, never a rule's input."""

    entity_ref: NonEmptyStr
    kind: Literal["LEGAL", "HEADQUARTERS", "OTHER"]
    address: NonEmptyStr
    country: NonEmptyStr | None = None


class DocEntityRow(EntityRow):
    """A holder a reviewer created from a document (Step 15), with where it is written."""

    document_sha256: NonEmptyStr
    page: int
    proposal_id: NonEmptyStr
    review_event: NonEmptyStr


class DocRelationshipRow(RelationshipRow):
    """A holding a reviewer accepted from a document (Step 15): the edge in the fixture
    shape, and the evidence a citation of it leads to."""

    document_sha256: NonEmptyStr
    page: int
    span_start: int
    span_end: int
    quote: NonEmptyStr
    proposal_id: NonEmptyStr
    reviewed_by: NonEmptyStr
    review_event: NonEmptyStr


def read_raw_rows[R: _Row](path: Path, model: type[R]) -> list[tuple[dict[str, str | None], R]]:
    """Read a fixture CSV as (cells as written, validated row) pairs.

    The header must be exactly the model's fields, in order. An empty cell is
    read as null. Bronze stores the cells; `model.model_validate(cells)` gives
    the row back.
    """
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        expected = list(model.model_fields)
        without = [name for name in expected if name not in model.OPTIONAL_COLUMNS]
        if reader.fieldnames not in (expected, without):
            raise ValueError(f"{path}: header is {reader.fieldnames}, expected {expected}")
        pairs = []
        for row in reader:
            if None in row:
                raise ValueError(f"{path}:{reader.line_num}: more cells than columns")
            raw = {key: value or None for key, value in row.items()}
            try:
                pairs.append((raw, model.model_validate(raw)))
            except ValidationError as exc:
                raise ValueError(f"{path}:{reader.line_num}: {exc}") from exc
        return pairs


def read_rows[R: _Row](path: Path, model: type[R]) -> list[R]:
    """Read a fixture CSV as validated rows. See `read_raw_rows`."""
    return [row for _, row in read_raw_rows(path, model)]
