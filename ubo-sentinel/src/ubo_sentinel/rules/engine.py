"""The rule engine: which entities are blocked, and what to recommend for the target.

It reads one `SubGraph` and the designations that reach it, and nothing else:
no database, no clock. Every threshold comes from the rule pack.

The legal test is a fix-point. An entity is blocked when the blocked entities
that own it directly hold enough of it; a stake held through an entity that is
not blocked does not count. Effective exposure is a risk signal beside that
test, never a substitute for it.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal, localcontext
from fractions import Fraction
from pathlib import Path

from ubo_sentinel.models.canonical import DECIMAL_QUANTUM
from ubo_sentinel.models.entity import Entity
from ubo_sentinel.models.evidence import (
    DataGap,
    EffectiveExposure,
    EvidencePath,
    GapCode,
    ReasonCode,
)
from ubo_sentinel.models.graph import SanctionMatch, SubGraph, upper_lower
from ubo_sentinel.models.provenance import Provenance
from ubo_sentinel.models.relationship import Relationship
from ubo_sentinel.models.rule_result import (
    BlockSet,
    DerivationBasis,
    NodeDerivation,
    ReasonFinding,
    RuleResult,
)
from ubo_sentinel.rules.pack import RulePack, load_rule_pack

OWNERSHIP_RELATIONS = frozenset({"OWNS", "CONSOLIDATED_BY"})
# An LEI in either state is no longer maintained, so what it says may be out of date.
UNMAINTAINED_STATUSES = frozenset({"LAPSED", "RETIRED"})

_RELATION_ORDER = {"OWNS": 0, "CONSOLIDATED_BY": 1, "CONTROLS": 2}
# The path search gives up after this many steps for each path it may return.
_STEPS_PER_PATH = 100


class PathLimitExceeded(RuntimeError):
    """The subgraph has more evidence paths than the rule pack allows one screen."""


@dataclass(frozen=True)
class Hop:
    """Every edge between one upper node and one lower node, read as one link.

    Parallel edges describe one holding: several sources, or a direct and an
    ultimate-parent record of the same pair. They are never added together.
    """

    upper: str
    lower: str
    edges: tuple[Relationship, ...]
    # The largest known direct stake, and the edge that states it.
    pct: Fraction | None
    stake_edge: Relationship | None
    # Ownership edges that say nothing about a direct stake: a majority is presumed.
    presumed_edges: tuple[Relationship, ...]
    control_edges: tuple[Relationship, ...]
    ownership: bool
    # True when only ultimate-parent records join the pair.
    ultimate_only: bool
    # The edge an evidence path cites for this hop.
    edge: Relationship

    @property
    def edge_ids(self) -> list[str]:
        return [edge.id for edge in self.edges]


def _hop(upper: str, lower: str, edges: list[Relationship]) -> Hop:
    edges = sorted(edges, key=lambda edge: edge.id)
    ownership = [edge for edge in edges if edge.rel_type in OWNERSHIP_RELATIONS]
    # An ultimate parent's percentage is not a direct stake in the entity.
    stakes = [edge for edge in ownership if edge.pct is not None and not edge.is_ultimate]
    stake_edge = max(stakes, key=lambda edge: (edge.pct, edge.id), default=None)
    cited = min(
        edges,
        key=lambda edge: (
            edge is not stake_edge,
            edge.is_ultimate,
            _RELATION_ORDER[edge.rel_type],
            edge.id,
        ),
    )
    return Hop(
        upper=upper,
        lower=lower,
        edges=tuple(edges),
        pct=Fraction(stake_edge.pct) if stake_edge else None,
        stake_edge=stake_edge,
        presumed_edges=tuple(edge for edge in ownership if edge not in stakes),
        control_edges=tuple(edge for edge in edges if edge.rel_type == "CONTROLS"),
        ownership=bool(ownership),
        ultimate_only=all(edge.is_ultimate for edge in edges),
        edge=cited,
    )


@dataclass(frozen=True)
class _Path:
    """A simple path from a blocked or possibly-blocked node down to the target."""

    start: str
    hops: tuple[Hop, ...]

    @property
    def nodes(self) -> list[str]:
        return [self.start, *(hop.lower for hop in self.hops)]

    def to_evidence(self) -> EvidencePath:
        return EvidencePath(node_ids=self.nodes, edge_ids=[hop.edge.id for hop in self.hops])


@dataclass
class _Members:
    """A blocked or possibly-blocked set, with why each node is in it."""

    block_set: BlockSet
    why: dict[str, NodeDerivation] = field(default_factory=dict)

    def add(
        self,
        node_id: str,
        basis: DerivationBasis,
        round_: int,
        aggregate: Fraction | None = None,
        edges: list[Relationship] | None = None,
        sanction_ids: list[str] | None = None,
    ) -> None:
        self.why[node_id] = NodeDerivation(
            node_id=node_id,
            block_set=self.block_set,
            basis=basis,
            round=round_,
            aggregate_pct=None if aggregate is None else _decimal(aggregate),
            edge_ids=[edge.id for edge in edges or []],
            sanction_ids=sanction_ids or [],
        )


def _decimal(value: Fraction) -> Decimal:
    """An exact value as a 4-place Decimal, for reporting. Tests compare the exact value.

    Rounded down, so a reported figure never reads as meeting a level it did not meet.
    """
    with localcontext() as context:
        context.prec = 60
        exact = Decimal(value.numerator) / Decimal(value.denominator)
        return exact.quantize(DECIMAL_QUANTUM, rounding=ROUND_DOWN)


class RuleEngine:
    def __init__(self, pack: RulePack) -> None:
        self.pack = pack
        self.rule_pack_hash = pack.rule_pack_hash

    @classmethod
    def load(cls, path: Path) -> "RuleEngine":
        return cls(load_rule_pack(path))

    def evaluate(self, subgraph: SubGraph, sanction_matches: list[SanctionMatch]) -> RuleResult:
        """Screen `subgraph.target_id`. Raises `PathLimitExceeded`."""
        return _Evaluation(self.pack, subgraph, sanction_matches).result()


def classify_match(pack: RulePack, match: SanctionMatch) -> BlockSet | None:
    if not match.is_active:
        return None
    if match.designated_entity_id == match.entity_id:
        # The node's own designation, however its record was joined to it.
        return "BLOCKED"
    if match.match_type != "FUZZY_NAME":
        # It crossed a link made of an identifier that the merge guard would not
        # merge on: the identifier is contested, so the match is neither proof nor nothing.
        return "POSSIBLY_BLOCKED"
    if match.confidence >= pack.match_threshold:
        return "BLOCKED"
    if match.confidence >= pack.review_band_low:
        return "POSSIBLY_BLOCKED"
    return None


class _Evaluation:
    """One run of the rules over one subgraph."""

    def __init__(self, pack: RulePack, subgraph: SubGraph, matches: list[SanctionMatch]) -> None:
        self.pack = pack
        self.subgraph = subgraph
        self.target = subgraph.target_id
        # Context below the target takes no part in the rules.
        self.upward = set(subgraph.depth_up)
        self.nodes: dict[str, Entity] = {n.id: n for n in subgraph.nodes if n.id in self.upward}
        self.matches = sorted(
            (match for match in matches if match.entity_id in self.upward),
            key=SanctionMatch.sort_key,
        )
        self.threshold = Fraction(pack.ownership_threshold_pct)

        pairs: dict[tuple[str, str], list[Relationship]] = defaultdict(list)
        for edge in subgraph.edges:
            upper, lower = upper_lower(edge)
            if upper in self.upward and lower in self.upward:
                pairs[upper, lower].append(edge)
        self.edges = [edge for edges in pairs.values() for edge in edges]
        self.owners: dict[str, list[Hop]] = defaultdict(list)
        self.assets: dict[str, list[Hop]] = defaultdict(list)
        for (upper, lower), edges in sorted(pairs.items()):
            hop = _hop(upper, lower, edges)
            self.owners[lower].append(hop)
            self.assets[upper].append(hop)

    # --- seeds -----------------------------------------------------------------------------

    def _seeds(self) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        """Node -> the sanction ids that seed it, for the blocked and the possibly-blocked set."""
        found: dict[BlockSet, dict[str, list[str]]] = {"BLOCKED": {}, "POSSIBLY_BLOCKED": {}}
        for match in self.matches:
            block_set = classify_match(self.pack, match)
            if block_set is not None:
                active = [record.id for record in match.sanction_records if record.is_active]
                found[block_set].setdefault(match.entity_id, []).extend(active)
        blocked = found["BLOCKED"]
        review = {n: ids for n, ids in found["POSSIBLY_BLOCKED"].items() if n not in blocked}
        return blocked, review

    # --- the fix-points --------------------------------------------------------------------

    def _meets(self, value: Fraction) -> bool:
        if self.pack.threshold_operator == ">":
            return value > self.threshold
        return value >= self.threshold

    def _stake(self, node_id: str, holders: set[str]) -> tuple[Fraction, list[Hop]]:
        """What `holders` own of the node directly, and the hops that say so."""
        hops = [h for h in self.owners[node_id] if h.upper in holders and h.pct is not None]
        if not hops:
            return Fraction(0), []
        if self.pack.aggregate_blocked_owners:
            return sum((hop.pct for hop in hops), Fraction(0)), hops
        largest = max(hop.pct for hop in hops)
        return largest, [hop for hop in hops if hop.pct == largest]

    def _blocked(self, seeds: dict[str, list[str]]) -> _Members:
        """The least set that holds the seeds and every entity they block in turn."""
        members = _Members("BLOCKED")
        for node_id, sanction_ids in sorted(seeds.items()):
            members.add(node_id, "SEED", 0, sanction_ids=sanction_ids)
        round_ = 0
        while True:
            round_ += 1
            holders = set(members.why)
            added = False
            # Every node is tested against the set as the round began, so the
            # round a node joins in does not depend on the order of the nodes.
            for node_id in sorted(self.upward - holders):
                aggregate, hops = self._stake(node_id, holders)
                if hops and self._meets(aggregate):
                    members.add(
                        node_id, "AGGREGATE", round_, aggregate, [hop.stake_edge for hop in hops]
                    )
                    added = True
            if not added:
                return members

    def _possibly_blocked(self, seeds: dict[str, list[str]], blocked: set[str]) -> _Members:
        members = _Members("POSSIBLY_BLOCKED")
        for node_id, sanction_ids in sorted(seeds.items()):
            members.add(node_id, "SEED", 0, sanction_ids=sanction_ids)
        round_ = 0
        while True:
            round_ += 1
            holders = blocked | set(members.why)
            added = False
            for node_id in sorted(self.upward - holders):
                presumed = [
                    edge
                    for hop in self.owners[node_id]
                    if hop.upper in holders
                    for edge in hop.presumed_edges
                ]
                if presumed and self.pack.presume_majority_for_consolidation:
                    members.add(node_id, "PRESUMED_MAJORITY", round_, edges=presumed)
                    added = True
                    continue
                aggregate, hops = self._stake(node_id, holders)
                if hops and self._meets(aggregate):
                    members.add(
                        node_id, "AGGREGATE", round_, aggregate, [hop.stake_edge for hop in hops]
                    )
                    added = True
            if not added:
                return members

    # --- paths -----------------------------------------------------------------------------

    def _paths(self, starts: set[str]) -> list[_Path]:
        """Every simple path from a start down to the target.

        Where a start reaches the target without an ultimate-parent shortcut,
        its paths that use one are left out: they restate the direct chain.
        """
        limit = self.pack.max_paths
        found: list[_Path] = []
        steps = 0
        for start in sorted(starts):
            if start == self.target:
                found.append(_Path(start, ()))
                continue
            own: list[_Path] = []
            stack: list[tuple[str, tuple[Hop, ...]]] = [(start, ())]
            while stack:
                node_id, hops = stack.pop()
                steps += 1
                if steps > limit * _STEPS_PER_PATH:
                    raise PathLimitExceeded(self._limit_message())
                seen = {start, *(hop.lower for hop in hops)}
                for hop in reversed(self.assets[node_id]):
                    if hop.lower in seen:
                        continue
                    if hop.lower == self.target:
                        own.append(_Path(start, (*hops, hop)))
                    else:
                        stack.append((hop.lower, (*hops, hop)))
                if len(found) + len(own) > limit:
                    raise PathLimitExceeded(self._limit_message())
            direct = [path for path in own if not any(hop.ultimate_only for hop in path.hops)]
            found.extend(direct or own)
        return found

    def _limit_message(self) -> str:
        return (
            f"The graph above {self.target} has more than {self.pack.max_paths} evidence paths"
            " (rule pack 'max_paths')."
        )

    def _exposure(
        self, paths: list[_Path], blocked: set[str]
    ) -> tuple[EffectiveExposure, Fraction | None, list[_Path]]:
        """Exposure, its exact sum, and the paths it was taken over.

        A path counts from the last blocked entity on it: that entity is blocked
        in full, so what blocked parties above it hold of it adds nothing.
        """
        if self.target in blocked:
            # The legal test has decided; exposure is a signal for targets it has not.
            return EffectiveExposure(), None, []
        counted = [
            path
            for path in paths
            if path.start in blocked
            and not any(hop.lower in blocked for hop in path.hops)
            and all(hop.ownership for hop in path.hops)
        ]
        values = []
        for path in counted:
            if all(hop.pct is not None for hop in path.hops):
                value = Fraction(100)
                for hop in path.hops:
                    value = value * hop.pct / 100
                values.append(value)
        total = sum(values, Fraction(0)) if values else None
        exposure = EffectiveExposure(
            max_pct=_decimal(max(values)) if values else None,
            sum_pct=None if total is None else _decimal(total),
            has_unknown=len(values) < len(counted),
        )
        return exposure, total, counted

    # --- gaps ------------------------------------------------------------------------------

    def _gaps(self, paths: list[_Path], blocked: set[str]) -> list[DataGap]:
        on_path_nodes = {node_id for path in paths for node_id in path.nodes}
        on_path_edges = {edge_id for path in paths for hop in path.hops for edge_id in hop.edge_ids}
        every = self.pack.gap_scope == "any"
        gaps: dict[tuple, DataGap] = {}

        def add(code: GapCode, subject_id: str, provenance: Provenance, relevant: bool) -> None:
            gap = DataGap(
                code=code, subject_id=subject_id, decision_relevant=relevant, provenance=provenance
            )
            gaps[gap.sort_key()] = gap

        for edge in self.edges:
            # A control link has no percentage to report, so its absence is not a gap.
            if edge.rel_type in OWNERSHIP_RELATIONS and edge.pct is None:
                add("NULL_PCT_EDGE", edge.id, edge.provenance, every or edge.id in on_path_edges)
        for record in self.subgraph.reporting_exceptions:
            if record.entity_id in self.upward:
                relevant = every or record.entity_id in on_path_nodes
                add("REPORTING_EXCEPTION", record.entity_id, record.provenance, relevant)
        for node_id, node in self.nodes.items():
            if node.status in UNMAINTAINED_STATUSES:
                add("LAPSED_LEI", node_id, node.provenance, every or node_id in on_path_nodes)
        for match in self.matches:
            in_band = classify_match(self.pack, match) == "POSSIBLY_BLOCKED"
            if in_band and match.entity_id not in blocked:
                record = next(r for r in match.sanction_records if r.is_active)
                relevant = every or match.entity_id in on_path_nodes
                add("REVIEW_BAND_MATCH", match.entity_id, record.provenance, relevant)
        for gap in self.subgraph.set_gaps:
            if gap.subject_id in self.upward:
                add(
                    gap.code,
                    gap.subject_id,
                    gap.provenance,
                    every or gap.subject_id in on_path_nodes,
                )
        # An owner beyond the depth limit is unknown, and unknown is never a silent CLEAR.
        for node_id in self.subgraph.truncated_at:
            add("DEPTH_LIMIT_REACHED", node_id, self.nodes[node_id].provenance, True)
        if self.pack.duplicate_link_review:
            for link in self.subgraph.links:
                # Only designations cross a link. The other record's owners are
                # outside this graph unless the record itself is in it.
                if (
                    link.node_id in self.upward
                    and link.linked_node_id not in self.upward
                    and link.linked_has_upstream
                    and link.confidence >= self.pack.review_band_low
                ):
                    add(
                        "POSSIBLE_DUPLICATE",
                        link.node_id,
                        self.nodes[link.node_id].provenance,
                        True,
                    )
        return list(gaps.values())

    # --- the recommendation ------------------------------------------------------------------

    def result(self) -> RuleResult:
        pack, target = self.pack, self.target
        blocked_seeds, review_seeds = self._seeds()
        blocked = self._blocked(blocked_seeds)
        in_b = set(blocked.why)
        # A node the legal test blocks is blocked, whatever a weaker match says of it.
        review_seeds = {n: ids for n, ids in review_seeds.items() if n not in in_b}
        possibly = self._possibly_blocked(review_seeds, in_b)
        in_p = set(possibly.why)

        paths = self._paths(in_b | in_p)
        exposure, exposure_sum, exposure_paths = self._exposure(paths, in_b)
        gaps = self._gaps(paths, in_b)
        aggregate, aggregate_hops = self._stake(target, in_b - {target})

        findings: list[ReasonFinding] = []

        def found(
            code: ReasonCode,
            subject_id: str = target,
            pct: Fraction | None = None,
            edges: Iterable[Relationship] = (),
            sanction_ids: Iterable[str] = (),
        ) -> None:
            findings.append(
                ReasonFinding(
                    code=code,
                    subject_id=subject_id,
                    pct=None if pct is None else _decimal(pct),
                    edge_ids=[edge.id for edge in edges],
                    sanction_ids=list(sanction_ids),
                )
            )

        if target in in_b:
            recommendation = "ESCALATE"
            stake, hops = aggregate, aggregate_hops
            if target in blocked_seeds:
                found("DIRECT_DESIGNATION", sanction_ids=blocked_seeds[target])
                # Would the target stay blocked if its own designation were lifted?
                others = {n: ids for n, ids in blocked_seeds.items() if n != target}
                without = set(self._blocked(others).why)
                stake, hops = self._stake(target, without - {target})
                hops = hops if target in without else []
            if hops:
                found("DERIVED_50PCT", pct=stake, edges=[hop.stake_edge for hop in hops])
        else:
            if target in in_p:
                why = possibly.why[target]
                findings.append(
                    ReasonFinding(
                        code="POSSIBLY_BLOCKED",
                        subject_id=target,
                        pct=why.aggregate_pct,
                        edge_ids=why.edge_ids,
                        sanction_ids=why.sanction_ids,
                    )
                )
            # The sum over paths: the largest single path understates a holding
            # that is split across vehicles.
            if exposure_sum is not None and exposure_sum >= Fraction(pack.review_exposure_pct):
                edges = [hop.edge for path in exposure_paths for hop in path.hops]
                found("EFFECTIVE_EXPOSURE", pct=exposure_sum, edges=edges)
            if aggregate_hops and aggregate >= Fraction(pack.near_miss_pct):
                found(
                    "NEAR_MISS_AGGREGATE",
                    pct=aggregate,
                    edges=[hop.stake_edge for hop in aggregate_hops],
                )
            for subject_id in sorted({gap.subject_id for gap in gaps if gap.decision_relevant}):
                found("DECISION_RELEVANT_GAP", subject_id=subject_id)
            if pack.control_link_review:
                on_path = {id(hop) for path in paths for hop in path.hops}
                for hops in self.owners.values():
                    for hop in hops:
                        if (
                            hop.control_edges
                            and hop.upper in in_b | in_p
                            and id(hop) in on_path
                            and (pack.control_link_scope == "upstream" or hop.lower == target)
                        ):
                            found("CONTROL_LINK", subject_id=hop.upper, edges=hop.control_edges)
            recommendation = "REVIEW" if findings else "CLEAR"

        seeds = set(blocked_seeds) | set(review_seeds)
        sources = {node.provenance.source for node in self.nodes.values()}
        sources.update(edge.provenance.source for edge in self.edges)
        sources.update(r.provenance.source for m in self.matches for r in m.sanction_records)
        return RuleResult(
            target_id=target,
            rule_pack_id=pack.id,
            rule_pack_hash=pack.rule_pack_hash,
            recommendation=recommendation,
            reasons=[finding.code for finding in findings],
            blocked_set=sorted(in_b),
            possibly_blocked_set=sorted(in_p),
            # A path is told from the designation that starts it.
            paths=[path.to_evidence() for path in paths if path.start in seeds],
            effective_exposure=exposure,
            data_gaps=gaps,
            aggregate_pct=_decimal(aggregate),
            findings=findings,
            derivations=[*blocked.why.values(), *possibly.why.values()],
            max_depth=self.subgraph.max_depth_up,
            nodes_checked=len(self.nodes),
            edges_checked=len(self.edges),
            sources_checked=sorted(sources),
        )
