"""Explainer Agent: the memo. Every claim cites the records it rests on.

`build_memo` is a pure function of what the screen read and what the rules
concluded: it derives nothing itself. `render_memo` turns a memo into Markdown.
The stored memo is unmasked; a role's memo is rendered from the masked memo
that `GraphView.mask_decision` returns.
"""

from decimal import Decimal
from functools import cache

import jinja2

from ubo_sentinel.agents.base import BaseAgent, PipelineState, ScreenContext
from ubo_sentinel.agents.traversal import screen_depth
from ubo_sentinel.graph import queries
from ubo_sentinel.models.decision import normalise_query
from ubo_sentinel.models.entity import Entity
from ubo_sentinel.models.evidence import DataGap, GapCode
from ubo_sentinel.models.graph import SanctionMatch, SubGraph, upper_lower
from ubo_sentinel.models.memo import (
    ActionCode,
    Citation,
    Claim,
    Coverage,
    DatasetRef,
    Memo,
    MemoEdge,
    MemoEntity,
    MemoMatch,
    NextAction,
    RulePackRef,
    WhatIf,
)
from ubo_sentinel.models.relationship import Relationship
from ubo_sentinel.models.rule_result import RuleResult
from ubo_sentinel.rules.engine import OWNERSHIP_RELATIONS, classify_match
from ubo_sentinel.rules.pack import RulePack

# What a reviewer can do about a gap that decides.
GAP_ACTIONS: dict[GapCode, ActionCode] = {
    "NULL_PCT_EDGE": "OBTAIN_SHAREHOLDING",
    "REPORTING_EXCEPTION": "OBTAIN_PARENT_INFORMATION",
    "LAPSED_LEI": "REFRESH_LEI_RECORD",
    "REVIEW_BAND_MATCH": "COMPARE_MATCH_RECORDS",
    "PCT_SUM_OVER_100": "RECONCILE_STAKES",
    "DEPTH_LIMIT_REACHED": "RESCREEN_DEEPER",
    "POSSIBLE_DUPLICATE": "SCREEN_LINKED_RECORD",
}

_DERIVATION_CLAIMS = {
    ("BLOCKED", "SEED"): "DESIGNATED",
    ("POSSIBLY_BLOCKED", "SEED"): "POSSIBLY_DESIGNATED",
    ("BLOCKED", "AGGREGATE"): "BLOCKED_BY_OWNERSHIP",
    ("POSSIBLY_BLOCKED", "AGGREGATE"): "POSSIBLY_BLOCKED_BY_OWNERSHIP",
    ("POSSIBLY_BLOCKED", "PRESUMED_MAJORITY"): "PRESUMED_MAJORITY",
}


def _pack_ref(pack: RulePack) -> RulePackRef:
    return RulePackRef(
        id=pack.id,
        hash=pack.rule_pack_hash,
        ownership_threshold_pct=pack.ownership_threshold_pct,
        threshold_operator=pack.threshold_operator,
        near_miss_pct=pack.near_miss_pct,
        review_exposure_pct=pack.review_exposure_pct,
        match_threshold=pack.match_threshold,
        review_band_low=pack.review_band_low,
    )


def _entity(node: Entity, depth: int | None) -> MemoEntity:
    return MemoEntity(
        id=node.id,
        legal_name=node.legal_name,
        aliases=node.aliases,
        jurisdiction=node.jurisdiction,
        entity_type=node.entity_type,
        status=node.status,
        lei=node.lei,
        registration_number=node.registration_number,
        depth=depth,
        provenance=node.provenance,
    )


def _dataset_citations(datasets: list[DatasetRef]) -> list[Citation]:
    return [Citation(kind="dataset", ref_id=item.snapshot_id) for item in datasets]


class _Evidence:
    """The records of one screen, by id, as citations."""

    def __init__(
        self, subgraph: SubGraph, matches: list[SanctionMatch], datasets: list[DatasetRef]
    ) -> None:
        self.upward = set(subgraph.depth_up)
        self.nodes = {node.id: node for node in subgraph.nodes if node.id in self.upward}
        self.edges: dict[str, Relationship] = {}
        for edge in subgraph.edges:
            if set(upper_lower(edge)) <= self.upward:
                self.edges[edge.id] = edge
        self.matches = [match for match in matches if match.entity_id in self.upward]
        self.sanctions = {
            record.id: record for match in self.matches for record in match.sanction_records
        }
        self.datasets = _dataset_citations(datasets)

    def node(self, node_id: str) -> Citation:
        return Citation(kind="node", ref_id=node_id, provenance=self.nodes[node_id].provenance)

    def edge(self, edge_id: str) -> Citation:
        return Citation(kind="edge", ref_id=edge_id, provenance=self.edges[edge_id].provenance)

    def sanction(self, sanction_id: str) -> Citation:
        record = self.sanctions[sanction_id]
        return Citation(kind="sanction", ref_id=sanction_id, provenance=record.provenance)

    def gap(self, gap: DataGap) -> Citation:
        """The record a gap is about: the node, the edge, or another record that says so."""
        subject, provenance = gap.subject_id, gap.provenance
        if subject in self.edges and self.edges[subject].provenance == provenance:
            return self.edge(subject)
        if subject in self.nodes and self.nodes[subject].provenance == provenance:
            return self.node(subject)
        for record in self.sanctions.values():
            if record.provenance == provenance:
                return self.sanction(record.id)
        return Citation(kind="record", ref_id=subject, provenance=provenance)


def build_memo(
    *,
    pack: RulePack,
    max_depth: int,
    datasets: list[DatasetRef],
    query: str = "",
    subgraph: SubGraph | None = None,
    matches: list[SanctionMatch] | None = None,
    rule_result: RuleResult | None = None,
    what_if: WhatIf | None = None,
    referenced: list[Entity] | None = None,
) -> Memo:
    """The memo of one screen.

    Without a subgraph the query matched nothing. With a subgraph and no rule
    result, the graph had more evidence paths than the pack allows.
    """
    if subgraph is None:
        return _memo_not_found(pack, max_depth, datasets, query)

    evidence = _Evidence(subgraph, matches or [], datasets)
    entities = [_entity(node, subgraph.depth_up[node.id]) for node in evidence.nodes.values()]
    entities += [_entity(node, None) for node in referenced or [] if node.id not in evidence.nodes]
    edges = [
        MemoEdge(
            id=edge.id,
            upper_id=upper_lower(edge)[0],
            lower_id=upper_lower(edge)[1],
            rel_type=edge.rel_type,
            pct=edge.pct,
            is_ultimate=edge.is_ultimate,
            provenance=edge.provenance,
        )
        for edge in evidence.edges.values()
    ]
    memo_matches = [
        MemoMatch(
            entity_id=match.entity_id,
            designated_entity_id=match.designated_entity_id,
            match_type=match.match_type,
            confidence=match.confidence,
            is_active=match.is_active,
            effect=classify_match(pack, match) or "NONE",
            programs=[record.program for record in match.sanction_records],
            list_sources=[record.list_source for record in match.sanction_records],
            citations=[evidence.sanction(record.id) for record in match.sanction_records],
        )
        for match in evidence.matches
    ]
    coverage = Coverage(
        max_depth=max_depth,
        depth_reached=max(subgraph.depth_up.values()),
        nodes_checked=len(evidence.nodes),
        edges_checked=len(evidence.edges),
        truncated_at=subgraph.truncated_at,
        down_truncated=subgraph.down_truncated,
        sources=sorted(
            {node.provenance.source for node in evidence.nodes.values()}
            | {edge.provenance.source for edge in evidence.edges.values()}
            | {record.provenance.source for record in evidence.sanctions.values()}
        ),
        datasets=datasets,
    )
    coverage_claim = Claim(
        code="COVERAGE",
        subject_id=subgraph.target_id,
        params={
            "max_depth": coverage.max_depth,
            "depth_reached": coverage.depth_reached,
            "nodes_checked": coverage.nodes_checked,
            "edges_checked": coverage.edges_checked,
        },
        citations=evidence.datasets,
    )
    common = {
        "target_id": subgraph.target_id,
        "entities": entities,
        "edges": edges,
        "matches": memo_matches,
        "coverage": coverage,
        "rule_pack": _pack_ref(pack),
    }

    if rule_result is None:
        limit = Claim(
            code="REASON",
            subject_id=subgraph.target_id,
            params={"reason": "EVIDENCE_LIMIT_EXCEEDED", "max_paths": pack.max_paths},
            citations=evidence.datasets,
        )
        return Memo(
            recommendation="REVIEW",
            reasons=["EVIDENCE_LIMIT_EXCEEDED"],
            claims=[limit, coverage_claim],
            next_actions=[NextAction(code="NARROW_SCREEN", subject_id=subgraph.target_id)],
            **common,
        )

    claims = [coverage_claim, *_claims(evidence, subgraph, rule_result)]
    return Memo(
        recommendation=rule_result.recommendation,
        reasons=rule_result.reasons,
        blocked_set=rule_result.blocked_set,
        possibly_blocked_set=rule_result.possibly_blocked_set,
        aggregate_pct=rule_result.aggregate_pct,
        effective_exposure=rule_result.effective_exposure,
        claims=claims,
        paths=rule_result.paths,
        what_if=what_if,
        next_actions=_next_actions(pack, evidence, subgraph, rule_result, memo_matches),
        **common,
    )


def _memo_not_found(pack: RulePack, max_depth: int, datasets: list[DatasetRef], query: str) -> Memo:
    claim = Claim(
        code="REASON",
        subject_id=None,
        params={"reason": "ENTITY_NOT_FOUND"},
        citations=_dataset_citations(datasets),
    )
    return Memo(
        target_id=None,
        query_norm=normalise_query(query),
        recommendation="REVIEW",
        reasons=["ENTITY_NOT_FOUND"],
        claims=[claim],
        coverage=Coverage(
            max_depth=max_depth,
            depth_reached=0,
            nodes_checked=0,
            edges_checked=0,
            datasets=datasets,
        ),
        next_actions=[NextAction(code="CHECK_QUERY", subject_id=None)],
        rule_pack=_pack_ref(pack),
    )


def _claims(evidence: _Evidence, subgraph: SubGraph, result: RuleResult) -> list[Claim]:
    target = subgraph.target_id
    claims: list[Claim] = []

    # Why each node is blocked or possibly blocked: the rule engine's own trace.
    for why in result.derivations:
        params = {} if why.basis == "SEED" else {"round": why.round}
        if why.aggregate_pct is not None:
            params["aggregate_pct"] = str(why.aggregate_pct)
        citations = [evidence.sanction(i) for i in why.sanction_ids]
        citations += [evidence.edge(i) for i in why.edge_ids]
        claims.append(
            Claim(
                code=_DERIVATION_CLAIMS[why.block_set, why.basis],
                subject_id=why.node_id,
                params=params,
                citations=citations,
            )
        )

    relevant = [gap for gap in result.data_gaps if gap.decision_relevant]
    for finding in result.findings:
        citations = [evidence.edge(i) for i in finding.edge_ids]
        citations += [evidence.sanction(i) for i in finding.sanction_ids]
        if finding.code == "DECISION_RELEVANT_GAP":
            citations += [
                evidence.gap(gap) for gap in relevant if gap.subject_id == finding.subject_id
            ]
        params = {"reason": finding.code}
        if finding.pct is not None:
            params["pct"] = str(finding.pct)
        claims.append(
            Claim(code="REASON", subject_id=finding.subject_id, params=params, citations=citations)
        )

    for gap in result.data_gaps:
        claims.append(
            Claim(
                code="DATA_GAP",
                subject_id=gap.subject_id,
                params={"gap": gap.code, "decision_relevant": gap.decision_relevant},
                citations=[evidence.gap(gap)],
            )
        )

    blocked = set(result.blocked_set)
    if result.aggregate_pct > 0:
        stakes = [
            edge
            for edge in evidence.edges.values()
            if upper_lower(edge)[1] == target
            and upper_lower(edge)[0] in blocked
            and edge.rel_type in OWNERSHIP_RELATIONS
            and edge.pct is not None
            and not edge.is_ultimate
        ]
        claims.append(
            Claim(
                code="DIRECT_AGGREGATE",
                subject_id=target,
                params={"aggregate_pct": str(result.aggregate_pct)},
                citations=[evidence.edge(edge.id) for edge in stakes],
            )
        )

    exposure = result.effective_exposure
    if exposure.sum_pct is not None or exposure.has_unknown:
        finding = next((f for f in result.findings if f.code == "EFFECTIVE_EXPOSURE"), None)
        edge_ids = (
            finding.edge_ids
            if finding
            else sorted({edge_id for path in result.paths for edge_id in path.edge_ids})
        )
        claims.append(
            Claim(
                code="EFFECTIVE_EXPOSURE",
                subject_id=target,
                params={
                    "max_pct": None if exposure.max_pct is None else str(exposure.max_pct),
                    "sum_pct": None if exposure.sum_pct is None else str(exposure.sum_pct),
                    "has_unknown": exposure.has_unknown,
                },
                citations=[evidence.edge(i) for i in edge_ids] or [evidence.node(target)],
            )
        )

    if not result.blocked_set and not result.possibly_blocked_set:
        claims.append(
            Claim(
                code="NO_DESIGNATION_FOUND",
                subject_id=target,
                params={"nodes_checked": result.nodes_checked},
                citations=evidence.datasets,
            )
        )
    return claims


def _next_actions(
    pack: RulePack,
    evidence: _Evidence,
    subgraph: SubGraph,
    result: RuleResult,
    matches: list[MemoMatch],
) -> list[NextAction]:
    actions: list[NextAction] = []
    for gap in result.data_gaps:
        if not gap.decision_relevant:
            continue
        code = GAP_ACTIONS[gap.code]
        if gap.code == "POSSIBLE_DUPLICATE":
            actions += [
                NextAction(code=code, subject_id=gap.subject_id, params={"linked_id": linked_id})
                for linked_id in unseen_duplicates(pack, subgraph, gap.subject_id)
            ]
        elif gap.code == "REVIEW_BAND_MATCH":
            actions += [
                NextAction(
                    code=code,
                    subject_id=gap.subject_id,
                    params={"designated_id": match.designated_entity_id},
                )
                for match in matches
                if match.entity_id == gap.subject_id and match.effect == "POSSIBLY_BLOCKED"
            ]
        elif gap.code == "DEPTH_LIMIT_REACHED":
            params = {"max_depth": subgraph.max_depth_up}
            actions.append(NextAction(code=code, subject_id=gap.subject_id, params=params))
        else:
            actions.append(NextAction(code=code, subject_id=gap.subject_id))
    return actions


def unseen_duplicates(pack: RulePack, subgraph: SubGraph, node_id: str) -> list[str]:
    """The records linked to a node whose owners the screened graph does not show."""
    return sorted(
        link.linked_node_id
        for link in subgraph.links
        if link.node_id == node_id
        and link.linked_node_id not in subgraph.depth_up
        and link.linked_has_upstream
        and link.confidence >= pack.review_band_low
    )


# --- rendering --------------------------------------------------------------------------


def format_pct(value: Decimal | str | None) -> str:
    """A percentage without trailing zeros; `?` when it is not known."""
    if value is None:
        return "?"
    return format(Decimal(str(value)).normalize(), "f")


def format_score(value: float) -> str:
    return format(value, ".2f")


def cite(citation: Citation) -> str:
    """A citation as text: the record, its source and its date."""
    if citation.provenance is None:
        return f"snapshot `{citation.ref_id}`"
    provenance = citation.provenance
    return f"`{provenance.source}:{provenance.source_record_id}` as of {provenance.as_of}"


@cache
def _environment() -> jinja2.Environment:
    environment = jinja2.Environment(
        loader=jinja2.PackageLoader("ubo_sentinel", "templates"),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,
    )
    environment.filters.update(pct=format_pct, score=format_score, cite=cite)
    return environment


def render_memo(memo: Memo) -> str:
    """The memo as Markdown. Names are bold, ids and codes are in backticks."""

    def name(entity_id: str | None) -> str:
        if entity_id is None:
            return "the query"
        try:
            entity = memo.entity(entity_id)
        except KeyError:
            return f"`{entity_id}`"
        return f"**{entity.legal_name}** (`{entity.id}`)"

    def edge(edge_id: str) -> str:
        item = memo.edge(edge_id)
        stake = "" if item.rel_type == "CONTROLS" else f" {format_pct(item.pct)} %"
        ultimate = " (ultimate parent)" if item.is_ultimate else ""
        return f"{name(item.upper_id)} `{item.rel_type}`{stake}{ultimate} → {name(item.lower_id)}"

    def subject(subject_id: str | None) -> str:
        if subject_id is not None and any(item.id == subject_id for item in memo.edges):
            return edge(subject_id)
        return name(subject_id)

    template = _environment().get_template("memo.md.j2")
    return template.render(memo=memo, name=name, edge=edge, subject=subject)


# --- the agent --------------------------------------------------------------------------


def datasets_of(ctx: ScreenContext) -> list[DatasetRef]:
    return [DatasetRef.model_validate(row) for row in ctx.store.snapshot_info()]


class ExplainerAgent(BaseAgent):
    name = "Explainer"
    provides = ("memo",)
    runs_after_halt = True

    def run(self, state: PipelineState, ctx: ScreenContext) -> PipelineState:
        subgraph, matches = state.subgraph, state.sanction_matches or []
        referenced: list[Entity] = []
        if subgraph is not None:
            # Entities the memo names that are outside the graph: the party a
            # designation belongs to, and a linked record with owners of its own.
            outside = {match.designated_entity_id for match in matches}
            for node_id in subgraph.depth_up:
                outside.update(unseen_duplicates(ctx.pack, subgraph, node_id))
            for entity_id in sorted(outside - set(subgraph.depth_up)):
                found = queries.get_entity_by_id(ctx.store, entity_id)
                if found is not None:
                    referenced.append(found)
        memo = build_memo(
            pack=ctx.pack,
            max_depth=screen_depth(state, ctx),
            datasets=datasets_of(ctx),
            query=state.request.query,
            subgraph=subgraph,
            matches=matches,
            rule_result=state.rule_result,
            what_if=state.what_if,
            referenced=referenced,
        )
        return state.with_(memo=memo)
