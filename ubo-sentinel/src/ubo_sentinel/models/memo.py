"""The risk memo: what a screen found, as claims that cite their evidence.

Names are held once, in the `entities` table; claims, paths and matches refer
to entities and edges by id. So a claim without a citation can be counted, and
masking a memo for a role is a change to one table and a mapping of ids.
"""

from datetime import date
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ubo_sentinel.models.decision import DecisionStatus, Recommendation, UtcDatetime
from ubo_sentinel.models.entity import EntityStatus, EntityType
from ubo_sentinel.models.evidence import (
    DataGap,
    EffectiveExposure,
    EvidencePath,
    Exposure,
    ReasonCode,
)
from ubo_sentinel.models.provenance import Confidence, NonEmptyStr, Provenance
from ubo_sentinel.models.relationship import Pct, RelType
from ubo_sentinel.models.sanction import MatchType

# Raise when a change alters the memo a screen produces. Part of `PIPELINE_VERSION`.
MEMO_SCHEMA_VERSION = 1

DISCLAIMER = (
    "A recommendation for a human reviewer, not a decision and not legal advice."
    " Rule packs must be validated by compliance counsel."
)

ClaimCode = Literal[
    # An active designation puts the entity in the blocked or the possibly-blocked set.
    "DESIGNATED",
    "POSSIBLY_DESIGNATED",
    # Its owners in the set hold enough of it.
    "BLOCKED_BY_OWNERSHIP",
    "POSSIBLY_BLOCKED_BY_OWNERSHIP",
    # An ownership edge without a percentage to such an owner.
    "PRESUMED_MAJORITY",
    # A rule that fired; `params.reason` is the reason code.
    "REASON",
    "DATA_GAP",
    # What blocked owners hold of the target directly.
    "DIRECT_AGGREGATE",
    "EFFECTIVE_EXPOSURE",
    "NO_DESIGNATION_FOUND",
    # How far the screen went and what it read.
    "COVERAGE",
]

# What a citation points at. `record` is any other source record (a reporting
# exception, a set-level gap); `dataset` is a whole snapshot, for a claim about
# what was not found.
CitationKind = Literal["node", "edge", "sanction", "record", "dataset"]

# What a reviewer can do about a reason or a gap. Advisory.
ActionCode = Literal[
    "SCREEN_LINKED_RECORD",
    "RESCREEN_DEEPER",
    "COMPARE_MATCH_RECORDS",
    "OBTAIN_SHAREHOLDING",
    "OBTAIN_PARENT_INFORMATION",
    "REFRESH_LEI_RECORD",
    "RECONCILE_STAKES",
    "CHECK_QUERY",
    "NARROW_SCREEN",
]

MatchEffect = Literal["BLOCKED", "POSSIBLY_BLOCKED", "NONE"]
# Parameters of a next action that name an entity.
ENTITY_PARAMS = ("linked_id", "designated_id")
ParamValue = str | int | bool | None


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Citation(_Model):
    kind: CitationKind
    # The id of the node, edge or sanction; for a dataset, its snapshot id.
    ref_id: NonEmptyStr
    # The source record. A dataset citation has none.
    provenance: Provenance | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        if (self.kind == "dataset") != (self.provenance is None):
            raise ValueError("a citation names a source record, unless it cites a dataset")
        return self

    def sort_key(self) -> tuple[str, str, tuple[str, ...]]:
        return (self.kind, self.ref_id, self.provenance.sort_key() if self.provenance else ())


def _sort_citations(value: list[Citation]) -> list[Citation]:
    return sorted({item.sort_key(): item for item in value}.values(), key=Citation.sort_key)


class Claim(_Model):
    """One statement of the memo, with the records it rests on."""

    code: ClaimCode
    # The node or edge the claim is about; None for a claim about the query itself.
    subject_id: NonEmptyStr | None
    # Percentages are 4-place strings, as everywhere in a canonical payload.
    params: dict[str, ParamValue] = {}
    citations: list[Citation] = []

    _sort = field_validator("citations")(_sort_citations)

    @field_validator("params")
    @classmethod
    def _sort_params(cls, value: dict[str, ParamValue]) -> dict[str, ParamValue]:
        return dict(sorted(value.items()))

    def sort_key(self) -> tuple[str, str, str]:
        kind = self.params.get("reason") or self.params.get("gap")
        return (self.code, self.subject_id or "", str(kind))


class MemoEntity(_Model):
    id: NonEmptyStr
    legal_name: NonEmptyStr
    aliases: list[str] = []
    jurisdiction: str | None = None
    entity_type: EntityType
    status: EntityStatus
    lei: str | None = None
    registration_number: str | None = None
    # Hops above the target; None for an entity the memo names that is outside the graph.
    depth: int | None = None
    provenance: Provenance


class MemoEdge(_Model):
    id: NonEmptyStr
    # Owner, parent or controller; and the other end.
    upper_id: NonEmptyStr
    lower_id: NonEmptyStr
    rel_type: RelType
    pct: Pct | None = None
    is_ultimate: bool = False
    provenance: Provenance


class MemoMatch(_Model):
    """The designations of one designated entity that reach one node, and what they do."""

    entity_id: NonEmptyStr
    designated_entity_id: NonEmptyStr
    match_type: MatchType
    confidence: Confidence
    is_active: bool
    effect: MatchEffect
    programs: list[str] = []
    list_sources: list[str] = []
    citations: list[Citation] = []

    _sort = field_validator("citations")(_sort_citations)

    @field_validator("programs", "list_sources")
    @classmethod
    def _sort_unique(cls, value: list[str]) -> list[str]:
        return sorted(set(value))

    def sort_key(self) -> tuple[str, str]:
        return (self.entity_id, self.designated_entity_id)


class DatasetRef(_Model):
    """One snapshot the screen read."""

    dataset: NonEmptyStr
    snapshot_id: NonEmptyStr
    row_count: int = Field(ge=0)
    as_of_min: date | None = None
    as_of_max: date | None = None
    published_at: date | None = None


class Coverage(_Model):
    """What was looked at, so a CLEAR still says how far the screen went."""

    max_depth: int = Field(ge=0)
    depth_reached: int = Field(ge=0)
    nodes_checked: int = Field(ge=0)
    edges_checked: int = Field(ge=0)
    truncated_at: list[str] = []
    down_truncated: bool = False
    sources: list[str] = []
    datasets: list[DatasetRef] = []

    @field_validator("truncated_at", "sources")
    @classmethod
    def _sort_unique(cls, value: list[str]) -> list[str]:
        return sorted(set(value))

    @field_validator("datasets")
    @classmethod
    def _sort_datasets(cls, value: list[DatasetRef]) -> list[DatasetRef]:
        return sorted(value, key=lambda item: item.dataset)


class ContestedMatch(_Model):
    entity_id: NonEmptyStr
    designated_entity_id: NonEmptyStr


class WhatIf(_Model):
    """What the rules conclude once the unsettled matches are settled, either way."""

    contested: list[ContestedMatch] = Field(min_length=1)
    if_confirmed: Recommendation
    if_rejected: Recommendation

    @field_validator("contested")
    @classmethod
    def _sort_contested(cls, value: list[ContestedMatch]) -> list[ContestedMatch]:
        return sorted(value, key=lambda item: (item.entity_id, item.designated_entity_id))


class NextAction(_Model):
    code: ActionCode
    subject_id: NonEmptyStr | None
    params: dict[str, ParamValue] = {}

    @field_validator("params")
    @classmethod
    def _sort_params(cls, value: dict[str, ParamValue]) -> dict[str, ParamValue]:
        return dict(sorted(value.items()))

    def sort_key(self) -> tuple[str, str, str]:
        return (self.code, self.subject_id or "", str(sorted(self.params.items())))


class RulePackRef(_Model):
    """The pack the rules ran with, and the thresholds a memo quotes."""

    id: NonEmptyStr
    hash: NonEmptyStr
    ownership_threshold_pct: Exposure
    threshold_operator: Literal[">=", ">"]
    near_miss_pct: Exposure
    review_exposure_pct: Exposure
    match_threshold: Confidence
    review_band_low: Confidence


class Memo(_Model):
    schema_version: int = MEMO_SCHEMA_VERSION
    target_id: NonEmptyStr | None
    # The normalised query, only when it matched nothing.
    query_norm: str | None = None
    # What the rules concluded. A guardrail failure raises the decision's
    # recommendation and leaves this as it was.
    recommendation: Recommendation
    reasons: list[ReasonCode] = []
    blocked_set: list[str] = []
    possibly_blocked_set: list[str] = []
    aggregate_pct: Exposure | None = None
    effective_exposure: EffectiveExposure = EffectiveExposure()
    entities: list[MemoEntity] = []
    edges: list[MemoEdge] = []
    claims: list[Claim] = []
    paths: list[EvidencePath] = []
    matches: list[MemoMatch] = []
    coverage: Coverage
    what_if: WhatIf | None = None
    next_actions: list[NextAction] = []
    rule_pack: RulePackRef
    disclaimer: str = DISCLAIMER

    @field_validator("reasons", "blocked_set", "possibly_blocked_set")
    @classmethod
    def _sort_unique[T: str](cls, value: list[T]) -> list[T]:
        return sorted(set(value))

    @field_validator("entities")
    @classmethod
    def _sort_entities(cls, value: list[MemoEntity]) -> list[MemoEntity]:
        return sorted(value, key=lambda item: item.id)

    @field_validator("edges")
    @classmethod
    def _sort_edges(cls, value: list[MemoEdge]) -> list[MemoEdge]:
        return sorted(value, key=lambda item: item.id)

    @field_validator("claims")
    @classmethod
    def _sort_claims(cls, value: list[Claim]) -> list[Claim]:
        return sorted(value, key=Claim.sort_key)

    @field_validator("paths")
    @classmethod
    def _sort_paths(cls, value: list[EvidencePath]) -> list[EvidencePath]:
        return sorted(value, key=EvidencePath.sort_key)

    @field_validator("matches")
    @classmethod
    def _sort_matches(cls, value: list[MemoMatch]) -> list[MemoMatch]:
        return sorted(value, key=MemoMatch.sort_key)

    @field_validator("next_actions")
    @classmethod
    def _sort_actions(cls, value: list[NextAction]) -> list[NextAction]:
        return sorted({item.sort_key(): item for item in value}.values(), key=NextAction.sort_key)

    def entity(self, entity_id: str) -> MemoEntity:
        for item in self.entities:
            if item.id == entity_id:
                return item
        raise KeyError(entity_id)

    def edge(self, edge_id: str) -> MemoEdge:
        for item in self.edges:
            if item.id == edge_id:
                return item
        raise KeyError(edge_id)

    def dangling(self) -> list[str]:
        """Ids the memo uses that its entity and edge tables do not hold."""
        entity_ids = {item.id for item in self.entities}
        edge_ids = {item.id for item in self.edges}
        used_entities = {*self.blocked_set, *self.possibly_blocked_set, *self.coverage.truncated_at}
        if self.target_id is not None:
            used_entities.add(self.target_id)
        used_edges: set[str] = set()
        for edge in self.edges:
            used_entities.update((edge.upper_id, edge.lower_id))
        for path in self.paths:
            used_entities.update(path.node_ids)
            used_edges.update(path.edge_ids)
        for match in self.matches:
            used_entities.update((match.entity_id, match.designated_entity_id))
        if self.what_if is not None:
            for item in self.what_if.contested:
                used_entities.update((item.entity_id, item.designated_entity_id))
        for citation in self.citations():
            if citation.kind == "node":
                used_entities.add(citation.ref_id)
            elif citation.kind == "edge":
                used_edges.add(citation.ref_id)
        subjects = {claim.subject_id for claim in self.claims}
        for action in self.next_actions:
            subjects.add(action.subject_id)
            used_entities.update(
                str(action.params[name]) for name in ENTITY_PARAMS if name in action.params
            )
        unknown_subjects = subjects - entity_ids - edge_ids - {None}
        return sorted((used_entities - entity_ids) | (used_edges - edge_ids) | unknown_subjects)

    def citations(self) -> list[Citation]:
        found = [citation for claim in self.claims for citation in claim.citations]
        found += [citation for match in self.matches for citation in match.citations]
        return found

    def uncited(self) -> list[str]:
        """The statements of the memo that cite nothing: `<code> <subject>` each."""
        missing = [f"{c.code} {c.subject_id or '-'}" for c in self.claims if not c.citations]
        missing += [f"MATCH {m.entity_id}" for m in self.matches if not m.citations]
        return missing


class DecisionView(_Model):
    """A decision as one role sees it: the memo typed, masked ids as pseudonyms.

    It is for display and export. It is not a `Decision`: a masked target id no
    longer gives the decision id, so nothing here is validated against it.
    """

    decision_id: NonEmptyStr
    query: str
    target_id: str | None
    recommendation: Recommendation
    status: DecisionStatus
    reasons: list[ReasonCode] = []
    blocked_set: list[str] = []
    possibly_blocked_set: list[str] = []
    paths: list[EvidencePath] = []
    effective_exposure: EffectiveExposure = EffectiveExposure()
    data_gaps: list[DataGap] = []
    guardrail_warnings: list[str] = []
    memo: Memo
    llm_annotations: dict[str, Any] = {}
    snapshot_set_id: NonEmptyStr
    snapshot_ids: dict[str, str]
    rule_pack_id: NonEmptyStr
    rule_pack_hash: NonEmptyStr
    engine_version: NonEmptyStr
    pipeline_version: int
    graph_digest: NonEmptyStr
    max_depth: int
    created_at: UtcDatetime
    reviewed_by: str | None = None
    review_reason: str | None = None
    reviewed_at: UtcDatetime | None = None
    # SHA-256 of the decision's canonical payload: the same for every role.
    payload_sha256: NonEmptyStr
    viewer_role: NonEmptyStr
    # True when the role sees some of the decision's entities masked.
    masked: bool = False


__all__ = [
    "DISCLAIMER",
    "MEMO_SCHEMA_VERSION",
    "ActionCode",
    "Citation",
    "Claim",
    "ClaimCode",
    "ContestedMatch",
    "Coverage",
    "DatasetRef",
    "DecisionView",
    "Memo",
    "MemoEdge",
    "MemoEntity",
    "MemoMatch",
    "NextAction",
    "RulePackRef",
    "WhatIf",
]
