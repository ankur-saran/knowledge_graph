"""The allow-listed query API. Agents, CLI and UI read the graph only through here.

Every function is registered in `QUERIES` by `@query`; there are no free-form
queries. Each takes a `GraphStore`, which fixes the snapshot set it reads.
"""

import inspect
import itertools
import json
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any

import networkx as nx

from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.graph import entity_resolution, projection
from ubo_sentinel.graph.store import GraphStore, QueryCall, id_list
from ubo_sentinel.models.change import EntityHistoryStep
from ubo_sentinel.models.decision import Decision
from ubo_sentinel.models.entity import Entity
from ubo_sentinel.models.evidence import EvidencePath
from ubo_sentinel.models.graph import (
    AddressPeer,
    AffectedEntity,
    CommonController,
    EntityMatch,
    HeldEdge,
    OwnerEdge,
    RepexRecord,
    SanctionMatch,
    SharedAddress,
    SiblingGroup,
    SubGraph,
    upper_lower,
)
from ubo_sentinel.models.provenance import Provenance
from ubo_sentinel.models.relationship import Relationship
from ubo_sentinel.models.sanction import Sanction
from ubo_sentinel.pipeline import differ, gold
from ubo_sentinel.pipeline.bronze import BRONZE_TABLES
from ubo_sentinel.pipeline.context import context_digest
from ubo_sentinel.pipeline.silver import fold_provenance

__all__ = [
    "QUERIES",
    "find_paths_between",
    "get_affected_by",
    "get_affected_entities",
    "get_common_controllers",
    "get_decision_by_id",
    "get_direct_owners",
    "get_entities_sharing_address",
    "get_entity_by_id",
    "get_entity_history",
    "get_ownership_subgraph",
    "get_reporting_exceptions",
    "get_sanctioned_nodes",
    "get_sibling_entities",
    "get_source_record",
    "resolve_entity",
]

# A place held by more entities than this is a formation agent's or a registered
# office: sharing it says little, so its peers are counted and not listed.
MASS_ADDRESS_LIMIT = 50
# How far `find_paths_between` walks, and how much of the graph it may read doing so.
MAX_PATH_DEPTH = 10
MAX_PATH_NODES = 5000


@dataclass(frozen=True)
class QuerySpec:
    name: str
    function: Callable[..., Any]
    # The typed signature, without the store.
    signature: inspect.Signature


# Name -> query. This is the allow-list.
QUERIES: dict[str, QuerySpec] = {}


def query[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """Register a function as an allow-listed query and log each call on its store."""

    @wraps(function)
    def call(store: GraphStore, *args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        result = function(store, *args, **kwargs)
        rows = len(result) if isinstance(result, list) else int(result is not None)
        store.calls.append(QueryCall(function.__name__, rows, time.perf_counter() - started))
        return result

    signature = inspect.signature(function)
    parameters = list(signature.parameters.values())[1:]
    QUERIES[function.__name__] = QuerySpec(
        function.__name__, call, signature.replace(parameters=parameters)
    )
    return call


@query
def resolve_entity(
    store: GraphStore,
    query: str,
    lei: str | None = None,
    registration_number: str | None = None,
    jurisdiction: str | None = None,
    max_candidates: int = 10,
) -> list[EntityMatch]:
    """Candidates for a name or an LEI, best first. Empty when nothing resembles it."""
    return entity_resolution.resolve(
        store, query, lei, registration_number, jurisdiction, max_candidates
    )


@query
def get_ownership_subgraph(
    store: GraphStore, entity_id: str, max_depth_up: int, max_depth_down: int
) -> SubGraph:
    """The entity, its owners and controllers up to `max_depth_up` hops, and context below."""
    return projection.extract_ego_graph(store, entity_id, max_depth_up, max_depth_down)


@query
def get_sanctioned_nodes(store: GraphStore, subgraph: SubGraph) -> list[SanctionMatch]:
    """The designations that reach a node of the subgraph, active or not.

    One match per node and designated entity. Which matches block is for the
    rule engine to say.
    """
    rows = store.rows(
        "SELECT id, node_id AS entity_id, designated_node_id AS designated_entity_id, program,"
        " list_date, list_source, is_active, match_type, match_confidence,"
        f" {', '.join(Provenance.model_fields)} FROM graph_sanctions"
        f" WHERE snapshot_set_id = ? AND node_id IN {id_list()} ORDER BY id",
        [store.set_id, [node.id for node in subgraph.nodes]],
    )
    groups: dict[tuple[str, str], list[Sanction]] = defaultdict(list)
    for row in rows:
        sanction = Sanction.model_validate(fold_provenance(row))
        groups[sanction.entity_id, sanction.designated_entity_id].append(sanction)
    ends = sorted({end for pair in groups for end in pair})
    jurisdiction = dict(
        store.con.execute(
            "SELECT id, jurisdiction FROM graph_nodes"
            f" WHERE snapshot_set_id = ? AND id IN {id_list()}",
            [store.set_id, ends],
        ).fetchall()
    )

    def agree(entity_id: str, designated_id: str) -> bool:
        if entity_id == designated_id:
            return True
        found = jurisdiction.get(entity_id)
        return found is not None and found == jurisdiction.get(designated_id)

    matches = [
        SanctionMatch(
            entity_id=entity_id,
            designated_entity_id=designated_id,
            match_type=records[0].match_type,
            confidence=records[0].match_confidence,
            is_active=any(record.is_active for record in records),
            jurisdictions_agree=agree(entity_id, designated_id),
            sanction_records=records,
        )
        for (entity_id, designated_id), records in groups.items()
    ]
    return sorted(matches, key=SanctionMatch.sort_key)


@query
def get_reporting_exceptions(store: GraphStore, entity_id: str) -> list[RepexRecord]:
    """Why the entity does not report a parent, if it says."""
    return projection.read_reporting_exceptions(store, [entity_id])


@query
def get_entity_by_id(store: GraphStore, entity_id: str) -> Entity | None:
    found = projection.read_nodes(store, [entity_id])
    return found[0] if found else None


@query
def get_direct_owners(store: GraphStore, entity_id: str) -> list[OwnerEdge]:
    """The owners, parents and controllers one hop above the entity."""
    edges = projection.read_edges(store, "lower_id = $2", [entity_id])
    upper = {edge.id: upper_lower(edge)[0] for edge in edges}
    owners = {node.id: node for node in projection.read_nodes(store, sorted(set(upper.values())))}
    found = [OwnerEdge(owner=owners[upper[edge.id]], relationship=edge) for edge in edges]
    return sorted(found, key=lambda item: (item.owner.id, item.relationship.id))


@query
def get_affected_entities(store: GraphStore, changed_node_id: str, max_depth: int) -> list[str]:
    """Every entity a change to this node's designations could reach.

    The node itself, the entities linked to it (a designation crosses one
    link), and everything they own or control down to `max_depth` hops, with
    the entities linked to each of those (`get_affected_by`).
    """
    return sorted({a.entity_id for a in _affected(store, [changed_node_id], max_depth)})


@query
def get_affected_by(store: GraphStore, node_ids: list[str], max_depth: int) -> list[AffectedEntity]:
    """For changes at several nodes at once: each entity reached, from which node, in how few hops.

    From each node: the node and the entities linked to it, then down the
    edges (owner, parent or controller to what it holds) one hop at a time to
    `max_depth`, and at every entity reached the entities linked to it. A link
    costs no hop and is not crossed twice in a row: a designation crosses one
    link, and a record's duplicate is screened beside it (`POSSIBLE_DUPLICATE`).
    One query per hop, whatever the number of nodes.
    """
    return _affected(store, node_ids, max_depth)


def _affected(store: GraphStore, node_ids: list[str], max_depth: int) -> list[AffectedEntity]:
    best: dict[tuple[str, str], int] = {}

    def take(pairs: set[tuple[str, str]], hops: int) -> set[tuple[str, str]]:
        new = {pair for pair in pairs if pair not in best}
        best.update(dict.fromkeys(new, hops))
        return new

    def neighbours(sql: str, pairs: set[tuple[str, str]]) -> set[tuple[str, str]]:
        found: dict[str, list[str]] = defaultdict(list)
        nodes = sorted({node for _, node in pairs})
        for near, far in store.con.execute(sql, [store.set_id, nodes]).fetchall():
            found[near].append(far)
        return {(origin, far) for origin, node in pairs for far in found.get(node, ())}

    links = (
        "SELECT node_id, linked_node_id FROM graph_links"
        f" WHERE snapshot_set_id = ? AND node_id IN {id_list()}"
    )
    below = (
        "SELECT DISTINCT upper_id, lower_id FROM graph_edges"
        f" WHERE snapshot_set_id = ? AND upper_id IN {id_list()}"
    )
    frontier = take({(node, node) for node in node_ids}, 0)
    frontier |= take(neighbours(links, frontier), 0)
    for hops in range(1, max_depth + 1):
        if not frontier:
            break
        reached = take(neighbours(below, frontier), hops)
        frontier = reached | take(neighbours(links, reached), hops)
    return [
        AffectedEntity(entity_id=entity, origin_id=origin, hops=hops)
        for (origin, entity), hops in sorted(best.items(), key=lambda item: item[0][::-1])
    ]


@query
def get_source_record(store: GraphStore, provenance: Provenance) -> dict[str, Any] | None:
    """The source record behind a citation, as published. None when there is no such record."""
    table = store.con.execute(
        "SELECT table_name FROM bronze_snapshots WHERE snapshot_id = ?", [provenance.snapshot_id]
    ).fetchone()
    if table is None or table[0] not in BRONZE_TABLES:
        return None
    row = store.con.execute(
        f"SELECT _raw FROM {table[0]}"
        " WHERE _snapshot_id = ? AND _source = ? AND _source_record_id = ?",
        [provenance.snapshot_id, provenance.source, provenance.source_record_id],
    ).fetchone()
    return json.loads(row[0]) if row else None


@query
def get_decision_by_id(
    store: GraphStore, decision_id: str, log: AuditLog | None = None
) -> Decision | None:
    """A recorded decision, with its review if it has one. None when there is no such decision.

    Decisions live in the audit log, not in the graph: the store only logs the
    call, and a decision made on another snapshot set is returned as well. The
    log is the caller's (`ubo eval` keeps a scratch one); by default, `UBO_VAR`'s.
    """
    return (log or AuditLog()).decision(decision_id)


# --- Step 14: what an investigation reads beyond the screened graph -----------------------
# Each fan-out takes a `limit`, says how many there were in all, and orders by id:
# on a registry a parent holds thousands of subsidiaries and a registered agent's
# address holds a hundred thousand companies.

_ADDRESS_PROVENANCE = (
    "arg_min({'source': source, 'source_record_id': source_record_id,"
    " 'snapshot_id': snapshot_id, 'as_of': as_of, 'confidence': confidence},"
    " (kind, source, source_record_id))"
)


@query
def get_entities_sharing_address(
    store: GraphStore, entity_id: str, limit: int = 50
) -> list[SharedAddress]:
    """Each place the entity's records state, and the other entities stated at it.

    Read from the context layer (`pipeline/context.py`), compared on the
    normalised address and the country. Raises `ContextNotBuilt`.
    """
    context_digest(store.con, store.set_id)
    own = store.con.execute(
        "SELECT address_norm, country, list(DISTINCT kind ORDER BY kind),"
        f" arg_min(address, (kind, source, source_record_id)), {_ADDRESS_PROVENANCE}"
        " FROM ctx_addresses WHERE snapshot_set_id = ? AND entity_id = ?"
        " GROUP BY address_norm, country ORDER BY address_norm, country",
        [store.set_id, entity_id],
    ).fetchall()
    found = []
    for address_norm, country, kinds, address, provenance in own:
        peers = store.con.execute(
            "SELECT entity_id, list(DISTINCT kind ORDER BY kind),"
            f" arg_min(address, (kind, source, source_record_id)), {_ADDRESS_PROVENANCE}"
            " FROM ctx_addresses WHERE snapshot_set_id = ? AND address_norm = ?"
            " AND country IS NOT DISTINCT FROM ? AND entity_id <> ?"
            " GROUP BY entity_id ORDER BY entity_id",
            [store.set_id, address_norm, country, entity_id],
        ).fetchall()
        mass = len(peers) > MASS_ADDRESS_LIMIT
        listed = [] if mass else peers[:limit]
        nodes = {node.id: node for node in projection.read_nodes(store, [p[0] for p in listed])}
        found.append(
            SharedAddress(
                kinds=kinds,
                address=address,
                address_norm=address_norm,
                country=country,
                provenance=Provenance.model_validate(provenance),
                total=len(peers),
                mass=mass,
                peers=[
                    AddressPeer(
                        entity=nodes[peer_id],
                        kinds=peer_kinds,
                        address=peer_address,
                        provenance=Provenance.model_validate(peer_provenance),
                    )
                    for peer_id, peer_kinds, peer_address, peer_provenance in listed
                ],
            )
        )
    return found


def _fund_manager(edges: list[Relationship]) -> bool:
    return any(edge.rel_type == "CONTROLS" and edge.basis == "other" for edge in edges)


@query
def get_common_controllers(
    store: GraphStore, entity_ids: list[str], limit: int = 50
) -> list[CommonController]:
    """Owners, parents and controllers that hold two or more of the entities directly.

    Ultimate-parent records are left out: every subsidiary of a group shares
    its ultimate parent, which says nothing about these entities in particular.
    """
    ids = sorted(set(entity_ids))
    uppers = [
        upper
        for (upper,) in store.con.execute(
            "SELECT upper_id FROM graph_edges"
            f" WHERE snapshot_set_id = ? AND NOT is_ultimate AND lower_id IN {id_list()}"
            " GROUP BY upper_id HAVING count(DISTINCT lower_id) >= 2 ORDER BY upper_id LIMIT ?",
            [store.set_id, ids, limit],
        ).fetchall()
    ]
    edges = projection.read_edges(
        store,
        f"NOT is_ultimate AND upper_id IN {id_list('$2')} AND lower_id IN {id_list('$3')}",
        [uppers, ids],
    )
    nodes = {node.id: node for node in projection.read_nodes(store, uppers)}
    found = []
    for upper in uppers:
        held = [edge for edge in edges if upper_lower(edge)[0] == upper]
        found.append(
            CommonController(
                controller=nodes[upper],
                held_ids=sorted({upper_lower(edge)[1] for edge in held}),
                edges=held,
                fund_manager=_fund_manager(held),
            )
        )
    return found


@query
def get_sibling_entities(store: GraphStore, entity_id: str, limit: int = 50) -> list[SiblingGroup]:
    """Each direct owner, parent or controller of the entity, and the other entities it holds.

    Direct records only, as in `get_common_controllers`.
    """
    above = projection.read_edges(store, "NOT is_ultimate AND lower_id = $2", [entity_id])
    found = []
    for upper in sorted({upper_lower(edge)[0] for edge in above}):
        (total,) = store.con.execute(
            "SELECT count(DISTINCT lower_id) FROM graph_edges"
            " WHERE snapshot_set_id = ? AND NOT is_ultimate AND upper_id = ? AND lower_id <> ?",
            [store.set_id, upper, entity_id],
        ).fetchone()
        below = [
            lower
            for (lower,) in store.con.execute(
                "SELECT DISTINCT lower_id FROM graph_edges WHERE snapshot_set_id = ?"
                " AND NOT is_ultimate AND upper_id = ? AND lower_id <> ? ORDER BY 1 LIMIT ?",
                [store.set_id, upper, entity_id, limit],
            ).fetchall()
        ]
        held = projection.read_edges(
            store,
            f"NOT is_ultimate AND upper_id = $2 AND lower_id IN {id_list('$3')}",
            [upper, below],
        )
        nodes = {node.id: node for node in projection.read_nodes(store, [upper, *below])}
        own = [edge for edge in above if upper_lower(edge)[0] == upper]
        found.append(
            SiblingGroup(
                parent=nodes[upper],
                # The stake edge first, as a path cites it (6.2).
                relationship=min(own, key=lambda e: (e.pct is None, e.id)),
                total=total,
                siblings=[
                    HeldEdge(entity=nodes[upper_lower(edge)[1]], relationship=edge) for edge in held
                ],
                fund_manager=_fund_manager(own),
            )
        )
    return found


@query
def find_paths_between(
    store: GraphStore, source_id: str, target_id: str, max_depth: int, max_paths: int = 50
) -> list[EvidencePath]:
    """Chains of direct edges between two entities, either way down, shortest first.

    A path runs from the holder down to the held, as an evidence path does.
    One edge stands for each pair of entities (a stake before a record without
    one). Raises `ValueError` for a depth above `MAX_PATH_DEPTH`.
    """
    if not 1 <= max_depth <= MAX_PATH_DEPTH:
        raise ValueError(f"max_depth must be 1 to {MAX_PATH_DEPTH}, not {max_depth}.")
    paths = []
    for start, end in ((source_id, target_id), (target_id, source_id)):
        graph = _down_from(store, start, max_depth)
        if end in graph:
            for nodes in nx.all_simple_paths(graph, start, end, cutoff=max_depth):
                edges = [graph.edges[a, b]["edge_id"] for a, b in itertools.pairwise(nodes)]
                paths.append(EvidencePath(node_ids=nodes, edge_ids=edges))
    paths.sort(key=lambda path: (len(path.node_ids), path.sort_key()))
    return paths[:max_paths]


def _down_from(store: GraphStore, start: str, max_depth: int) -> nx.DiGraph:
    """The direct edges below `start` to `max_depth` hops, one per pair, in id order."""
    best: dict[tuple[str, str], tuple[bool, str]] = {}
    seen, frontier = {start}, [start]
    for _ in range(max_depth):
        if not frontier or len(seen) > MAX_PATH_NODES:
            break
        rows = store.con.execute(
            "SELECT upper_id, lower_id, pct IS NULL, id FROM graph_edges"
            f" WHERE snapshot_set_id = ? AND NOT is_ultimate AND upper_id IN {id_list()}",
            [store.set_id, frontier],
        ).fetchall()
        for upper, lower, no_stake, edge_id in rows:
            pair = (upper, lower)
            best[pair] = min(best.get(pair, (no_stake, edge_id)), (no_stake, edge_id))
        frontier = sorted({lower for _, lower, _, _ in rows} - seen)
        seen.update(frontier)
    graph = nx.DiGraph()
    for (upper, lower), (_, edge_id) in sorted(best.items()):
        graph.add_edge(upper, lower, edge_id=edge_id)
    return graph


@query
def get_entity_history(
    store: GraphStore, entity_id: str, max_sets: int = 5
) -> list[EntityHistoryStep]:
    """What changed about the entity across the built sets of the store's family, newest first.

    Each step compares one set with the next newer one, back from the store's
    own set through `max_sets` older sets, on the fields the rules read
    (`differ.diff_entity`). Two sets built by other code are a step that says
    so. The one query that reads sets other than its store's.
    """
    older = [
        set_id
        for (set_id,) in store.con.execute(
            "SELECT snapshot_set_id FROM snapshot_set_info"
            " WHERE family = ? AND seq < ? ORDER BY seq DESC",
            [store.snapshot_set.family, store.snapshot_set.seq],
        ).fetchall()
        if gold.is_built(store.con, set_id)
    ][:max_sets]
    steps, curr = [], store.set_id
    for prev in older:
        try:
            changes = differ.diff_entity(store.con, prev, curr, entity_id)
            steps.append(EntityHistoryStep(prev_set_id=prev, curr_set_id=curr, changes=changes))
        except differ.DiffRefused:
            steps.append(
                EntityHistoryStep(
                    prev_set_id=prev,
                    curr_set_id=curr,
                    changes=[],
                    refused="the two sets were built by other code",
                )
            )
        curr = prev
    return steps
