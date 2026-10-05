"""What the query API returns: a subgraph around one entity, and the matches on it."""

from typing import Any, Literal, Self

import networkx as nx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ubo_sentinel.models.entity import Entity, EntityStatus, EntityType
from ubo_sentinel.models.evidence import GapCode
from ubo_sentinel.models.provenance import Confidence, NonEmptyStr, Provenance
from ubo_sentinel.models.relationship import Relationship
from ubo_sentinel.models.reporting_exception import ReportingException
from ubo_sentinel.models.sanction import MatchType, Sanction

# How a query reached a candidate. Identifiers come before names.
ResolveType = Literal["LEI", "REGISTRATION", "NAME_EXACT", "NAME_FUZZY"]

RepexRecord = ReportingException


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SetGap(_Model):
    """A gap that belongs to the snapshot set, not to one screen."""

    code: GapCode
    # A node id or an edge id.
    subject_id: NonEmptyStr
    detail: dict[str, Any] = {}
    provenance: Provenance


class EntityLink(_Model):
    """Another entity that may be the same one as `node_id`. The two are never merged."""

    node_id: NonEmptyStr
    linked_node_id: NonEmptyStr
    match_type: MatchType
    confidence: Confidence
    linked_name: NonEmptyStr
    # True when the linked entity has owners or controllers of its own: ownership
    # that a subgraph around `node_id` does not show.
    linked_has_upstream: bool


class EntityMatch(_Model):
    """A candidate for a query, with what an analyst needs to pick one."""

    entity_id: NonEmptyStr
    confidence: Confidence
    match_type: ResolveType
    legal_name: NonEmptyStr
    # The name or alias the query matched.
    matched_name: NonEmptyStr
    jurisdiction: str | None = None
    entity_type: EntityType
    status: EntityStatus
    lei: str | None = None


class SanctionMatch(_Model):
    """The designations of one designated entity that reach one node."""

    entity_id: NonEmptyStr
    match_type: MatchType
    confidence: Confidence
    # True when at least one of the designations is active.
    is_active: bool
    # Equal to `entity_id` unless the designations crossed a link.
    designated_entity_id: NonEmptyStr
    sanction_records: list[Sanction] = Field(min_length=1)

    @field_validator("sanction_records")
    @classmethod
    def _sort_records(cls, value: list[Sanction]) -> list[Sanction]:
        return sorted(value, key=lambda sanction: sanction.id)

    def sort_key(self) -> tuple[str, str]:
        return (self.entity_id, self.designated_entity_id)


class OwnerEdge(_Model):
    """An owner, parent or controller of an entity, and the edge that says so."""

    owner: Entity
    relationship: Relationship


class SubGraph(_Model):
    """The graph around one target: everything a screen reads.

    `depth_up` holds the target (0) and every owner, parent and controller
    within `max_depth_up` hops, with the hop at which each was first reached.
    `down_ids` are context only: what the target owns or controls.
    """

    target_id: NonEmptyStr
    snapshot_set_id: NonEmptyStr
    max_depth_up: int = Field(ge=0)
    max_depth_down: int = Field(ge=0)
    nodes: list[Entity]
    edges: list[Relationship] = []
    depth_up: dict[str, int]
    down_ids: list[str] = []
    # Nodes at `max_depth_up` with an owner or controller outside the subgraph.
    truncated_at: list[str] = []
    # True when the target owns or controls more than the subgraph shows.
    down_truncated: bool = False
    reporting_exceptions: list[ReportingException] = []
    set_gaps: list[SetGap] = []
    links: list[EntityLink] = []

    @field_validator("nodes")
    @classmethod
    def _sort_nodes(cls, value: list[Entity]) -> list[Entity]:
        return sorted(value, key=lambda node: node.id)

    @field_validator("edges")
    @classmethod
    def _sort_edges(cls, value: list[Relationship]) -> list[Relationship]:
        return sorted(value, key=lambda edge: edge.id)

    @field_validator("depth_up")
    @classmethod
    def _sort_depths(cls, value: dict[str, int]) -> dict[str, int]:
        return dict(sorted(value.items()))

    @field_validator("down_ids", "truncated_at")
    @classmethod
    def _sort_unique(cls, value: list[str]) -> list[str]:
        return sorted(set(value))

    @field_validator("reporting_exceptions")
    @classmethod
    def _sort_exceptions(cls, value: list[ReportingException]) -> list[ReportingException]:
        return sorted(value, key=lambda record: (record.entity_id, record.provenance.sort_key()))

    @field_validator("set_gaps")
    @classmethod
    def _sort_gaps(cls, value: list[SetGap]) -> list[SetGap]:
        return sorted(value, key=lambda gap: (gap.code, gap.subject_id))

    @field_validator("links")
    @classmethod
    def _sort_links(cls, value: list[EntityLink]) -> list[EntityLink]:
        return sorted(value, key=lambda link: (link.node_id, link.linked_node_id))

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        ids = [node.id for node in self.nodes]
        known = set(ids)
        if len(ids) != len(known):
            raise ValueError("a subgraph holds each node once")
        if self.depth_up.get(self.target_id) != 0:
            raise ValueError("the target is the node at depth 0")
        placed = set(self.depth_up) | set(self.down_ids)
        if placed != known:
            raise ValueError(f"nodes and their placement differ: {sorted(placed ^ known)}")
        for edge in self.edges:
            if edge.subject_id not in known or edge.object_id not in known:
                raise ValueError(f"edge {edge.id} has an end outside the subgraph")
        if not set(self.truncated_at) <= set(self.depth_up):
            raise ValueError("a truncated node is one of the upward nodes")
        return self

    @property
    def upward_ids(self) -> list[str]:
        """The target and its owners, parents and controllers. The rules read only these."""
        return list(self.depth_up)

    def node(self, entity_id: str) -> Entity:
        for node in self.nodes:
            if node.id == entity_id:
                return node
        raise KeyError(entity_id)

    def to_networkx(self) -> nx.MultiDiGraph:
        """Nodes and edges as a graph. Every edge runs upper -> lower, keyed by its id."""
        graph = nx.MultiDiGraph()
        for node in self.nodes:
            graph.add_node(node.id, entity=node, depth_up=self.depth_up.get(node.id))
        for edge in self.edges:
            upper, lower = upper_lower(edge)
            graph.add_edge(upper, lower, key=edge.id, relationship=edge)
        return graph


def upper_lower(edge: Relationship) -> tuple[str, str]:
    """(owner, parent or controller; the other end). A child is consolidated by its parent."""
    if edge.rel_type == "CONSOLIDATED_BY":
        return edge.object_id, edge.subject_id
    return edge.subject_id, edge.object_id
