"""Role-scoped views: what each role may see of the graph.

A `GraphView` wraps a store and offers the query API with the role's rules
applied. The screening pipeline reads the store itself, unmasked, so that a
decision does not depend on who asked; everything shown to a person, exported
or sent to a model goes through a view.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import duckdb

from ubo_sentinel.graph import queries
from ubo_sentinel.graph.store import GraphStore, id_list
from ubo_sentinel.models.canonical import short_id
from ubo_sentinel.models.entity import Entity
from ubo_sentinel.models.graph import (
    EntityLink,
    EntityMatch,
    OwnerEdge,
    RepexRecord,
    SanctionMatch,
    SubGraph,
)
from ubo_sentinel.models.ontology import OntologyConfig
from ubo_sentinel.models.provenance import Provenance
from ubo_sentinel.models.relationship import Relationship
from ubo_sentinel.models.sanction import Sanction

MASK = "[PII MASKED]"

# Roles that see what was built and nothing of the graph itself. The ontology
# has no property for this, so it is the one rule here that names a role.
METADATA_ONLY_ROLES = frozenset({"engineer"})


class GraphView:
    def __init__(self, store: GraphStore, role: str, ontology: OntologyConfig) -> None:
        if role not in ontology.roles:
            raise PermissionError(f"Unknown role '{role}'.")
        self.store = store
        self.role = role
        self._spec = ontology.roles[role]
        # Label -> the properties this role does not see.
        self._masked_fields = {
            label: set(fields) for label, fields in self._spec.masked_fields.items()
        }
        # Node id -> label, for the ids met so far.
        self._labels: dict[str, str] = {}
        # Pseudonym -> node id, for the pseudonyms this view has handed out.
        self._real_ids: dict[str, str] = {}

    @classmethod
    @contextmanager
    def open(
        cls,
        role: str,
        snapshot: str,
        ontology: OntologyConfig,
        con: duckdb.DuckDBPyConnection | None = None,
    ) -> Iterator["GraphView"]:
        with GraphStore.open(snapshot, con) as store:
            yield cls(store, role, ontology)

    @classmethod
    def time_travel(
        cls,
        role: str,
        snapshot_set_id: str,
        ontology: OntologyConfig,
        con: duckdb.DuckDBPyConnection | None = None,
    ):
        """A view on one explicit set id: the graph as a past decision read it."""
        return cls.open(role, snapshot_set_id, ontology, con)

    # --- what the role may do -----------------------------------------------------------

    def require_write(self) -> None:
        """Raise unless the role may write (a decision, a review, an audit event)."""
        if self._spec.read_only:
            raise PermissionError(f"Role '{self.role}' is read-only.")

    def build_info(self) -> dict[str, Any]:
        """What was built for the set. Every role may read it."""
        return self.store.build_info()

    def _require_graph(self) -> None:
        if self.role in METADATA_ONLY_ROLES:
            raise PermissionError(f"Role '{self.role}' sees pipeline metadata only.")

    # --- the query API ------------------------------------------------------------------

    def resolve_entity(
        self,
        query: str,
        lei: str | None = None,
        registration_number: str | None = None,
        jurisdiction: str | None = None,
        max_candidates: int = 10,
    ) -> list[EntityMatch]:
        self._require_graph()
        found = queries.resolve_entity(
            self.store, query, lei, registration_number, jurisdiction, max_candidates
        )
        self._labels.update({match.entity_id: match.entity_type for match in found})
        return [self._mask_match(match) for match in found]

    def get_ownership_subgraph(
        self, entity_id: str, max_depth_up: int, max_depth_down: int
    ) -> SubGraph:
        self._require_graph()
        subgraph = queries.get_ownership_subgraph(
            self.store, self._real(entity_id), max_depth_up, max_depth_down
        )
        return self.mask_subgraph(subgraph)

    def get_sanctioned_nodes(self, subgraph: SubGraph) -> list[SanctionMatch]:
        """`subgraph` is one this view returned; its ids may be pseudonyms."""
        self._require_graph()
        real = subgraph.model_copy(
            update={
                "nodes": [n.model_copy(update={"id": self._real(n.id)}) for n in subgraph.nodes]
            }
        )
        found = queries.get_sanctioned_nodes(self.store, real)
        self._learn([m.entity_id for m in found] + [m.designated_entity_id for m in found])
        return sorted((self._mask_sanction_match(m) for m in found), key=SanctionMatch.sort_key)

    def get_reporting_exceptions(self, entity_id: str) -> list[RepexRecord]:
        self._require_graph()
        found = queries.get_reporting_exceptions(self.store, self._real(entity_id))
        self._learn([record.entity_id for record in found])
        return [self._mask_exception(record) for record in found]

    def get_entity_by_id(self, entity_id: str) -> Entity | None:
        self._require_graph()
        found = queries.get_entity_by_id(self.store, self._real(entity_id))
        return self._mask_entity(found) if found else None

    def get_direct_owners(self, entity_id: str) -> list[OwnerEdge]:
        self._require_graph()
        entity_id = self._real(entity_id)
        found = queries.get_direct_owners(self.store, entity_id)
        self._learn([entity_id, *(item.owner.id for item in found)])
        return [
            OwnerEdge(
                owner=self._mask_entity(item.owner), relationship=self._mask_edge(item.relationship)
            )
            for item in found
        ]

    def get_affected_entities(self, changed_node_id: str, max_depth: int) -> list[str]:
        self._require_graph()
        found = queries.get_affected_entities(self.store, self._real(changed_node_id), max_depth)
        self._learn(found)
        return sorted(self._id(node_id) for node_id in found)

    def get_source_record(self, provenance: Provenance) -> dict[str, Any] | None:
        """The published record, or a stub when it concerns an entity this role sees masked."""
        self._require_graph()
        record = queries.get_source_record(self.store, provenance)
        if record is None or not self._masked_fields:
            return record
        if self._concerns_masked(provenance):
            return {
                "masked": True,
                "source": provenance.source,
                "snapshot_id": provenance.snapshot_id,
                "as_of": provenance.as_of.isoformat(),
            }
        return record

    # --- masking ------------------------------------------------------------------------

    def pseudonym(self, entity_id: str, label: str) -> str:
        """A stable stand-in for a masked node's id.

        An unsalted hash: it keeps a person's name off the screen, it is not
        anonymisation.
        """
        alias = f"{label.lower()}:{short_id([entity_id])}"
        self._real_ids[alias] = entity_id
        return alias

    def mask_subgraph(self, subgraph: SubGraph) -> SubGraph:
        """The subgraph as this role sees it. Unchanged for a role that masks nothing."""
        self._labels.update({node.id: node.entity_type for node in subgraph.nodes})
        if not self._masked_fields:
            return subgraph
        self._learn([link.linked_node_id for link in subgraph.links])
        return SubGraph(
            target_id=self._id(subgraph.target_id),
            snapshot_set_id=subgraph.snapshot_set_id,
            max_depth_up=subgraph.max_depth_up,
            max_depth_down=subgraph.max_depth_down,
            nodes=[self._mask_entity(node) for node in subgraph.nodes],
            edges=[self._mask_edge(edge) for edge in subgraph.edges],
            depth_up={self._id(node_id): depth for node_id, depth in subgraph.depth_up.items()},
            down_ids=[self._id(node_id) for node_id in subgraph.down_ids],
            truncated_at=[self._id(node_id) for node_id in subgraph.truncated_at],
            down_truncated=subgraph.down_truncated,
            reporting_exceptions=[self._mask_exception(r) for r in subgraph.reporting_exceptions],
            set_gaps=[
                gap.model_copy(update={"subject_id": self._id(gap.subject_id)})
                for gap in subgraph.set_gaps
            ],
            links=[self._mask_link(link) for link in subgraph.links],
        )

    def _is_masked(self, entity_id: str) -> bool:
        return self._labels.get(entity_id) in self._masked_fields

    def _id(self, entity_id: str) -> str:
        if self._is_masked(entity_id):
            return self.pseudonym(entity_id, self._labels[entity_id])
        return entity_id

    def _real(self, entity_id: str) -> str:
        """The node id behind a pseudonym this view handed out; any other id as it is."""
        return self._real_ids.get(entity_id, entity_id)

    def _learn(self, ids: list[str]) -> None:
        """Look up the label of each id not met before. Only a masking role needs them."""
        unknown = sorted(set(ids) - set(self._labels))
        if not unknown or not self._masked_fields:
            return
        rows = self.store.con.execute(
            f"SELECT id, label FROM graph_nodes WHERE snapshot_set_id = ? AND id IN {id_list()}",
            [self.store.set_id, unknown],
        ).fetchall()
        self._labels.update(dict(rows))

    def _mask_provenance(self, provenance: Provenance) -> Provenance:
        # The record id of a masked node is its identity.
        return provenance.model_copy(update={"source_record_id": MASK})

    def _mask_entity(self, entity: Entity) -> Entity:
        self._labels[entity.id] = entity.entity_type
        hidden = self._masked_fields.get(entity.entity_type)
        if not hidden:
            return entity
        update: dict[str, Any] = {
            "id": self.pseudonym(entity.id, entity.entity_type),
            "provenance": self._mask_provenance(entity.provenance),
            "supporting_records": [self._mask_provenance(p) for p in entity.supporting_records],
        }
        for name in hidden:
            value = getattr(entity, name)
            if isinstance(value, list):
                update[name] = [MASK] if value else []
            elif value is not None:
                update[name] = MASK
        return entity.model_copy(update=update)

    def _mask_edge(self, edge: Relationship) -> Relationship:
        return edge.model_copy(
            update={"subject_id": self._id(edge.subject_id), "object_id": self._id(edge.object_id)}
        )

    def _mask_exception(self, record: RepexRecord) -> RepexRecord:
        return record.model_copy(update={"entity_id": self._id(record.entity_id)})

    def _mask_link(self, link: EntityLink) -> EntityLink:
        return link.model_copy(
            update={
                "node_id": self._id(link.node_id),
                "linked_node_id": self._id(link.linked_node_id),
                "linked_name": MASK if self._is_masked(link.linked_node_id) else link.linked_name,
            }
        )

    def _mask_match(self, match: EntityMatch) -> EntityMatch:
        if match.entity_type not in self._masked_fields:
            return match
        return match.model_copy(
            update={
                "entity_id": self.pseudonym(match.entity_id, match.entity_type),
                "legal_name": MASK,
                "matched_name": MASK,
            }
        )

    def _mask_sanction(self, sanction: Sanction) -> Sanction:
        update: dict[str, Any] = {
            "entity_id": self._id(sanction.entity_id),
            "designated_entity_id": self._id(sanction.designated_entity_id),
        }
        # The record of a masked party's designation names that party.
        if self._is_masked(sanction.designated_entity_id):
            update["provenance"] = self._mask_provenance(sanction.provenance)
        return sanction.model_copy(update=update)

    def _mask_sanction_match(self, match: SanctionMatch) -> SanctionMatch:
        return match.model_copy(
            update={
                "entity_id": self._id(match.entity_id),
                "designated_entity_id": self._id(match.designated_entity_id),
                "sanction_records": [self._mask_sanction(s) for s in match.sanction_records],
            }
        )

    def _concerns_masked(self, provenance: Provenance) -> bool:
        """True when the cited record describes, or points at, a node this role sees masked."""
        key = [self.store.set_id, provenance.source, provenance.source_record_id,
               provenance.snapshot_id]  # fmt: skip
        where = "snapshot_set_id = ? AND source = ? AND source_record_id = ? AND snapshot_id = ?"
        con = self.store.con
        ids = {
            entity_id
            for (entity_id,) in con.execute(
                f"SELECT entity_id FROM silver_entity_xref WHERE {where}", key
            ).fetchall()
        }
        ids.update(
            entity_id
            for (entity_id,) in con.execute(
                f"SELECT designated_node_id FROM graph_sanctions WHERE {where}", key
            ).fetchall()
        )
        ids.update(
            entity_id
            for (entity_id,) in con.execute(
                f"SELECT entity_id FROM silver_reporting_exceptions WHERE {where}", key
            ).fetchall()
        )
        # An edge cites the record that decided it and every record that supports it.
        cited = json.dumps(provenance.source_record_id, ensure_ascii=False)
        for subject_id, object_id, supporting in con.execute(
            "SELECT subject_id, object_id, supporting_records FROM graph_edges"
            " WHERE snapshot_set_id = ? AND contains(CAST(supporting_records AS VARCHAR), ?)",
            [self.store.set_id, cited],
        ).fetchall():
            if any(
                (r["source"], r["source_record_id"], r["snapshot_id"]) == tuple(key[1:])
                for r in json.loads(supporting)
            ):
                ids.update((subject_id, object_id))
        self._learn(sorted(ids))
        return any(self._is_masked(entity_id) for entity_id in ids)
