"""Subgraphs built by hand, for tests of the rule engine. No database."""

from datetime import date
from decimal import Decimal

from ubo_sentinel.models import (
    Entity,
    EntityLink,
    Provenance,
    Relationship,
    ReportingException,
    Sanction,
    SanctionMatch,
    SetGap,
    SubGraph,
    relationship_id,
    sanction_id,
)
from ubo_sentinel.models.graph import upper_lower

PROVENANCE = Provenance(
    source="hand",
    source_record_id="r1",
    snapshot_id="s1",
    as_of=date(2026, 1, 1),
    confidence=1.0,
)

_BASIS = {
    "OWNS": "shareholding",
    "CONSOLIDATED_BY": "accounting_consolidation",
    "CONTROLS": "directorship",
}


def edge(upper, lower, pct=None, rel="OWNS", ultimate=False):
    """An edge named by its upper and lower end, whatever the relation's own direction."""
    subject, obj = (lower, upper) if rel == "CONSOLIDATED_BY" else (upper, lower)
    return Relationship(
        id=relationship_id(subject, obj, rel, ultimate),
        subject_id=subject,
        object_id=obj,
        rel_type=rel,
        pct=None if pct is None else Decimal(str(pct)),
        basis=_BASIS[rel],
        is_ultimate=ultimate,
        provenance=PROVENANCE,
    )


def upward_depths(target, edges):
    """Node -> hops above the target, for every node that owns or controls it."""
    owners = {}
    for item in edges:
        upper, lower = upper_lower(item)
        owners.setdefault(lower, set()).add(upper)
    depths, frontier = {target: 0}, [target]
    while frontier:
        found = {u for node in frontier for u in owners.get(node, ()) if u not in depths}
        depths.update(dict.fromkeys(found, depths[frontier[0]] + 1))
        frontier = sorted(found)
    return depths


def graph(target, edges, statuses=None, **fields):
    """A subgraph around `target`. Nodes that do not reach the target are context below it."""
    depths = upward_depths(target, edges)
    ids = {target} | {end for item in edges for end in (item.subject_id, item.object_id)}
    nodes = [
        Entity(
            id=node_id,
            legal_name=node_id,
            status=(statuses or {}).get(node_id, "ACTIVE"),
            entity_type="LegalEntity",
            provenance=PROVENANCE,
        )
        for node_id in ids
    ]
    return SubGraph(
        target_id=target,
        snapshot_set_id="hand",
        max_depth_up=len(ids),
        max_depth_down=1,
        nodes=nodes,
        edges=edges,
        depth_up=depths,
        down_ids=sorted(ids - set(depths)),
        **fields,
    )


def designated(node_id, by=None, confidence=1.0, match_type=None, active=True):
    """A designation that reaches `node_id`: its own, or one of `by` that crossed a link."""
    by = by or node_id
    match_type = match_type or ("DIRECT" if by == node_id else "FUZZY_NAME")
    record = Sanction(
        id=sanction_id("hand", f"des-{by}", node_id),
        entity_id=node_id,
        designated_entity_id=by,
        program="P",
        list_source="L",
        is_active=active,
        match_type=match_type,
        match_confidence=confidence,
        provenance=PROVENANCE.model_copy(update={"source_record_id": f"des-{by}"}),
    )
    return SanctionMatch(
        entity_id=node_id,
        designated_entity_id=by,
        match_type=match_type,
        confidence=confidence,
        is_active=active,
        sanction_records=[record],
    )


def reporting_exception(node_id):
    return ReportingException(
        entity_id=node_id, category="DIRECT_PARENT", reason="NON_PUBLIC", provenance=PROVENANCE
    )


def over_100(node_id):
    return SetGap(code="PCT_SUM_OVER_100", subject_id=node_id, provenance=PROVENANCE)


def link(node_id, linked, confidence=1.0, has_upstream=True, match_type="FUZZY_NAME"):
    return EntityLink(
        node_id=node_id,
        linked_node_id=linked,
        match_type=match_type,
        confidence=confidence,
        linked_name=linked,
        linked_has_upstream=has_upstream,
    )
