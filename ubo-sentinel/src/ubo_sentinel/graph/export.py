"""The Gold graph of one snapshot set as plain rows, for export (Step 13.2).

Read here, under `graph/`, because only this package reads Gold directly. The
rows are what a role may see: a role that masks a label gets that label's nodes
under their pseudonyms with the masked properties replaced, as `GraphView`
shows them. Nothing reads an export back: it is a copy for exploration, never
an input to screening.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ubo_sentinel.graph.store import GraphStore
from ubo_sentinel.graph.views import MASK, METADATA_ONLY_ROLES, pseudonym_for
from ubo_sentinel.models.ontology import OntologyConfig

PROVENANCE_COLUMNS = ("source", "source_record_id", "snapshot_id", "as_of", "confidence")
NODE_COLUMNS = (
    "id",
    "label",
    "legal_name",
    "aliases",
    "lei",
    "registration_authority_id",
    "registration_number",
    "jurisdiction",
    "status",
    "lei_status",
    "entity_type",
    "repex_reason",
    *PROVENANCE_COLUMNS,
)
EDGE_COLUMNS = (
    "id",
    "rel_type",
    "subject_id",
    "object_id",
    "upper_id",
    "lower_id",
    "pct",
    "basis",
    "is_ultimate",
    *PROVENANCE_COLUMNS,
)
# `designated_entity_id` is Gold's `designated_node_id`: the ontology's name, which
# an export carries. `entity_id` is the node the designation reaches (Gold `node_id`).
DESIGNATION_COLUMNS = (
    "id",
    "entity_id",
    "designated_entity_id",
    "program",
    "list_date",
    "list_source",
    "is_active",
    "match_type",
    "match_confidence",
    *PROVENANCE_COLUMNS,
)
LINK_COLUMNS = ("node_id", "linked_node_id", "match_type", "confidence", "name", "linked_name")


@dataclass(frozen=True)
class GraphRows:
    """One set's graph as the role sees it, each table in key order."""

    snapshot_set_id: str
    gold_digest: str
    role: str
    masked: bool
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    designations: list[dict[str, Any]]
    # One row per pair (Gold stores a link from each end).
    links: list[dict[str, Any]]

    def counts(self) -> dict[str, int]:
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "designations": len(self.designations),
            "links": len(self.links),
        }


def _plain(value: Any) -> Any:
    """A value as text-safe JSON would hold it: dates as ISO text, Decimal as a string."""
    if isinstance(value, Decimal):
        return format(value.quantize(Decimal("0.0001")), "f")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _select(store: GraphStore, table: str, columns: str, order: str) -> list[dict[str, Any]]:
    rows = store.rows(
        f"SELECT {columns} FROM {table} WHERE snapshot_set_id = ? ORDER BY {order}",
        [store.set_id],
    )
    return [{key: _plain(value) for key, value in row.items()} for row in rows]


def read_graph(store: GraphStore, role: str, ontology: OntologyConfig) -> GraphRows:
    """Every node, edge, designation and link of the store's set, as `role` may see them.

    Raises `PermissionError` for an unknown role or one that sees metadata only.
    """
    if role not in ontology.roles:
        raise PermissionError(f"Unknown role '{role}'.")
    if role in METADATA_ONLY_ROLES:
        raise PermissionError(f"Role '{role}' sees pipeline metadata only.")
    hidden = {label: set(fields) for label, fields in ontology.roles[role].masked_fields.items()}
    provenance = ", ".join(PROVENANCE_COLUMNS)
    nodes = _select(store, "graph_nodes", ", ".join(NODE_COLUMNS), "id")
    edges = _select(store, "graph_edges", ", ".join(EDGE_COLUMNS), "id")
    designations = _select(
        store,
        "graph_sanctions",
        "id, node_id AS entity_id, designated_node_id AS designated_entity_id, program,"
        f" list_date, list_source, is_active, match_type, match_confidence, {provenance}",
        "id",
    )
    links = _select(
        store,
        "graph_links",
        ", ".join(LINK_COLUMNS),
        "node_id, linked_node_id",
    )
    links = [row for row in links if row["node_id"] < row["linked_node_id"]]
    for row in nodes:
        row["aliases"] = sorted(row["aliases"] or [])

    ids = {
        row["id"]: pseudonym_for(row["id"], row["label"]) for row in nodes if row["label"] in hidden
    }
    if ids:
        _mask(nodes, edges, designations, links, ids, hidden)
    return GraphRows(
        snapshot_set_id=store.set_id,
        gold_digest=store.gold_digest,
        role=role,
        masked=bool(ids),
        nodes=sorted(nodes, key=lambda row: row["id"]),
        edges=edges,
        designations=designations,
        links=sorted(links, key=lambda row: (row["node_id"], row["linked_node_id"])),
    )


def _mask(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    designations: list[dict[str, Any]],
    links: list[dict[str, Any]],
    ids: dict[str, str],
    hidden: dict[str, set[str]],
) -> None:
    """Mask in place, as `GraphView` does: a masked node's id, masked properties and own
    record id; the ends of every edge and link; a masked party's designation records."""
    for row in nodes:
        if row["id"] not in ids:
            continue
        for name in hidden[row["label"]]:
            if isinstance(row.get(name), list):
                row[name] = [MASK] if row[name] else []
            elif row.get(name) is not None:
                row[name] = MASK
        row["id"] = ids[row["id"]]
        row["source_record_id"] = MASK
    for row in edges:
        for name in ("subject_id", "object_id", "upper_id", "lower_id"):
            row[name] = ids.get(row[name], row[name])
    for row in designations:
        if row["designated_entity_id"] in ids:
            row["source_record_id"] = MASK
        for name in ("entity_id", "designated_entity_id"):
            row[name] = ids.get(row[name], row[name])
    for row in links:
        for end, name in (("node_id", "name"), ("linked_node_id", "linked_name")):
            if row[end] in ids:
                row[end], row[name] = ids[row[end]], MASK


def masked_values(rows: GraphRows, store: GraphStore, ontology: OntologyConfig) -> list[str]:
    """What the role must not find anywhere in its export: the ids, masked properties and
    record ids of the nodes it sees masked, read from Gold unmasked."""
    hidden = {k: set(v) for k, v in ontology.roles[rows.role].masked_fields.items()}
    values: set[str] = set()
    for row in store.rows("SELECT * FROM graph_nodes WHERE snapshot_set_id = ?", [store.set_id]):
        if row["label"] not in hidden:
            continue
        values.update((row["id"], row["source_record_id"]))
        for name in hidden[row["label"]]:
            value = row.get(name)
            values.update(value if isinstance(value, list) else [value] if value else [])
    return sorted(values)
