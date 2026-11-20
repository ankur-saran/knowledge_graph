"""The rule engine: which entities are blocked, and what to recommend for the target.

It reads one `SubGraph` and the designations that reach it, and nothing else:
no database, no clock. Every threshold comes from the rule pack.

The legal test is a fix-point. An entity is blocked when the blocked entities
that own it directly hold enough of it; a stake held through an entity that is
not blocked does not count. Effective exposure is a risk signal beside that
test, never a substitute for it.

A stake is an interval (Step 16): a point, or a range a source states ("more
than 25 % but not more than 50 %"). An entity is blocked only when every value
of its holders' stake meets the test, and possibly blocked when only some do.
A pack tests one or more measures (shares, votes); an entity meets the test on
any one of them, and measures are never added together.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_UP, Decimal, localcontext
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
    StakeCoverage,
)
from ubo_sentinel.models.sanction import Sanction
from ubo_sentinel.rules.pack import RulePack, load_rule_pack

OWNERSHIP_RELATIONS = frozenset({"OWNS", "CONSOLIDATED_BY"})
# An LEI record in one of these states is no longer kept up to date, or was
# replaced or withdrawn, so what it says of the entity may be out of date.
UNMAINTAINED_LEI_STATUSES = frozenset({"LAPSED", "RETIRED", "MERGED", "DUPLICATE", "ANNULLED"})

_RELATION_ORDER = {"OWNS": 0, "CONSOLIDATED_BY": 1, "CONTROLS": 2}
# The path search gives up after this many steps for each path it may return.
_STEPS_PER_PATH = 100


class PathLimitExceeded(RuntimeError):
    """The subgraph has more evidence paths than the rule pack allows one screen."""


# --- stakes ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Stake:
    """A stake as an interval of exact values, each end in it or not.

    A point is a closed interval of width 0. On a point every test below gives
    the answer the one figure gave before ranges.
    """

    low: Fraction
    high: Fraction
    low_closed: bool = True
    high_closed: bool = True

    @classmethod
    def point(cls, value: Fraction) -> "Stake":
        return cls(value, value)

    @classmethod
    def of(cls, edge: Relationship) -> "Stake | None":
        """The edge's stake; None when it states none."""
        if edge.pct_low is None or edge.pct_high is None or edge.pct_bounds is None:
            return None
        bounds = edge.pct_bounds
        return cls(
            Fraction(edge.pct_low),
            Fraction(edge.pct_high),
            bounds[0] == "[",
            bounds[1] == "]",
        )

    @property
    def is_point(self) -> bool:
        return self.low == self.high

    @property
    def bounds(self) -> str:
        return ("[" if self.low_closed else "(") + ("]" if self.high_closed else ")")

    def every_meets(self, operator: str, threshold: Fraction) -> bool:
        """True when every value of the stake meets `operator threshold`."""
        if operator == ">":
            return self.low > threshold or (self.low == threshold and not self.low_closed)
        return self.low >= threshold

    def some_meets(self, operator: str, threshold: Fraction) -> bool:
        """True when some value of the stake meets `operator threshold`."""
        if operator == ">":
            return self.high > threshold
        return self.high > threshold or (self.high == threshold and self.high_closed)

    def rank(self) -> tuple[Fraction, bool, Fraction, bool]:
        """The order of size: the high end first, then the low end."""
        return (self.high, self.high_closed, self.low, self.low_closed)


def stake_sum(stakes: list[Stake]) -> Stake:
    """Lows add and highs add; an end is closed only if every addend's is."""
    return Stake(
        sum((item.low for item in stakes), Fraction(0)),
        sum((item.high for item in stakes), Fraction(0)),
        all(item.low_closed for item in stakes),
        all(item.high_closed for item in stakes),
    )


def stake_max(stakes: list[Stake]) -> Stake:
    """The largest low and the largest high, each closed if any stake at that value is."""
    low = max(item.low for item in stakes)
    high = max(item.high for item in stakes)
    return Stake(
        low,
        high,
        any(item.low_closed for item in stakes if item.low == low),
        any(item.high_closed for item in stakes if item.high == high),
    )


def stake_product(stakes: list[Stake]) -> Stake:
    """What a chain of stakes comes to of the last entity, in percent."""
    low, high = Fraction(100), Fraction(100)
    for item in stakes:
        low, high = low * item.low / 100, high * item.high / 100
    return Stake(
        low,
        high,
        all(item.low_closed for item in stakes),
        all(item.high_closed for item in stakes),
    )


def _largest(found: Iterable[tuple[Stake, Relationship]]) -> tuple[Stake, Relationship] | None:
    return max(found, key=lambda item: (item[0].rank(), item[1].id), default=None)


@dataclass(frozen=True)
class Hop:
    """Every edge between one upper node and one lower node, read as one link.

    Parallel edges describe one holding: several sources, or a direct and an
    ultimate-parent record of the same pair. They are never added together. A
    hop holds one stake for each measure the pack tests: the largest stated on it.
    """

    upper: str
    lower: str
    edges: tuple[Relationship, ...]
    # (measure, the largest known direct stake on it, the edge that states it).
    stakes: tuple[tuple[str, Stake, Relationship], ...]
    # Ownership edges that say nothing about a direct stake: a majority is presumed.
    presumed_edges: tuple[Relationship, ...]
    # `CONTROLS` edges, and stakes of a measure the pack does not test that could
    # meet its threshold or are of unknown size (votes under an equity-only pack).
    control_edges: tuple[Relationship, ...]
    ownership: bool
    # True when only ultimate-parent records join the pair.
    ultimate_only: bool
    # The edge an evidence path cites for this hop.
    edge: Relationship

    @property
    def edge_ids(self) -> list[str]:
        return [edge.id for edge in self.edges]

    def stake(self, measure: str) -> tuple[Stake, Relationship] | None:
        for name, stake, edge in self.stakes:
            if name == measure:
                return stake, edge
        return None

    @property
    def largest(self) -> tuple[Stake, Relationship] | None:
        """The largest stake on any measure: what exposure multiplies along a path."""
        return _largest((stake, edge) for _, stake, edge in self.stakes)


def _hop(upper: str, lower: str, edges: list[Relationship], pack: RulePack) -> Hop | None:
    """The hop the pack reads between two nodes; None when it reads none of their edges."""
    measures = pack.ownership_measures
    threshold = Fraction(pack.ownership_threshold_pct)
    edges = sorted(edges, key=lambda edge: edge.id)
    ownership = [edge for edge in edges if edge.rel_type in OWNERSHIP_RELATIONS]
    measured = [edge for edge in ownership if edge.interest in measures]
    unmeasured_control = []
    for edge in ownership:
        if edge.interest in measures:
            continue
        stake = Stake.of(edge)
        # A stake the pack does not test is a control link when it could meet the
        # threshold, or is of unknown size; one too small to is nothing to the pack.
        if stake is None or stake.some_meets(pack.threshold_operator, threshold):
            unmeasured_control.append(edge)
    kept = [
        edge
        for edge in edges
        if edge.rel_type not in OWNERSHIP_RELATIONS
        or edge in measured
        or edge in unmeasured_control
    ]
    if not kept:
        return None
    # An ultimate parent's percentage is not a direct stake in the entity.
    stated = [edge for edge in measured if edge.stated and not edge.is_ultimate]
    stakes = []
    for measure in measures:
        found = _largest(
            (stake, edge)
            for edge in stated
            if edge.interest == measure and (stake := Stake.of(edge)) is not None
        )
        if found is not None:
            stakes.append((measure, *found))
    largest = _largest((stake, edge) for _, stake, edge in stakes)
    stake_edge = largest[1] if largest else None
    cited = min(
        kept,
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
        edges=tuple(kept),
        stakes=tuple(stakes),
        presumed_edges=tuple(edge for edge in measured if edge not in stated),
        control_edges=tuple(
            edge for edge in kept if edge.rel_type == "CONTROLS" or edge in unmeasured_control
        ),
        ownership=bool(measured),
        ultimate_only=all(edge.is_ultimate for edge in kept),
        edge=cited,
    )


@dataclass(frozen=True)
class _Holding:
    """What some holders own of one node directly on one measure, and the hops that say so."""

    measure: str
    stake: Stake
    hops: tuple[Hop, ...]

    @property
    def edges(self) -> list[Relationship]:
        return [found[1] for hop in self.hops if (found := hop.stake(self.measure))]


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


def _decimal(value: Fraction, up: bool = False) -> Decimal:
    """An exact value as a 4-place Decimal, for reporting. Tests compare the exact value.

    A figure, or the low end of a range, is rounded down, so it never reads as
    meeting a level it did not meet; the high end of a range is rounded up, so
    it never reads as short of one it reaches.
    """
    with localcontext() as context:
        context.prec = 60
        exact = Decimal(value.numerator) / Decimal(value.denominator)
        return exact.quantize(DECIMAL_QUANTUM, rounding=ROUND_UP if up else ROUND_DOWN)


def _reported(stake: Stake | None) -> tuple[Decimal | None, Decimal | None, str | None]:
    """(the figure, or a range's low end; a range's high end; its bounds) for reporting."""
    if stake is None:
        return None, None, None
    if stake.is_point:
        return _decimal(stake.low), None, None
    return _decimal(stake.low), _decimal(stake.high, up=True), stake.bounds


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
        stake: Stake | None = None,
        edges: list[Relationship] | None = None,
        sanction_ids: list[str] | None = None,
    ) -> None:
        low, high, bounds = _reported(stake)
        self.why[node_id] = NodeDerivation(
            node_id=node_id,
            block_set=self.block_set,
            basis=basis,
            round=round_,
            aggregate_pct=low,
            aggregate_pct_high=high,
            pct_bounds=bounds,
            edge_ids=[edge.id for edge in edges or []],
            sanction_ids=sanction_ids or [],
        )


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


def in_scope(pack: RulePack, match: SanctionMatch) -> list[Sanction]:
    """The match's active designations on a list the pack applies."""
    lists = set(pack.designation_lists)
    return [r for r in match.sanction_records if r.is_active and r.list_source in lists]


def other_regime(pack: RulePack, match: SanctionMatch) -> list[Sanction]:
    """The match's active designations on lists the pack does not apply."""
    lists = set(pack.designation_lists)
    return [r for r in match.sanction_records if r.is_active and r.list_source not in lists]


def classify_match(pack: RulePack, match: SanctionMatch) -> BlockSet | None:
    """What the match does under the pack: only its designations on the pack's lists count."""
    return _classify(pack, match) if in_scope(pack, match) else None


def classify_other_regime(pack: RulePack, match: SanctionMatch) -> BlockSet | None:
    """What the match's designations on other lists would do under the pack's test."""
    return _classify(pack, match) if other_regime(pack, match) else None


def _classify(pack: RulePack, match: SanctionMatch) -> BlockSet | None:
    """How strongly the match ties a designation to its node, whatever the list."""
    if match.designated_entity_id == match.entity_id:
        # The node's own designation, however its record was joined to it.
        return "BLOCKED"
    if match.match_type != "FUZZY_NAME":
        # It crossed a link made of an identifier that the merge guard would not
        # merge on: the identifier is contested, so the match is neither proof nor nothing.
        return "POSSIBLY_BLOCKED"
    if match.confidence >= pack.match_threshold:
        if pack.name_match_requires_jurisdiction and not match.jurisdictions_agree:
            # A name and nothing else in common: a homonym abroad is as likely.
            return "POSSIBLY_BLOCKED"
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
            hop = _hop(upper, lower, edges, pack)
            if hop is not None:
                self.owners[lower].append(hop)
                self.assets[upper].append(hop)

    # --- seeds -----------------------------------------------------------------------------

    def _seeds(
        self, other_lists: bool = False
    ) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        """Node -> the sanction ids that seed it, for the blocked and the possibly-blocked set.

        The designations on the pack's lists, or with `other_lists` those on the
        lists it does not apply.
        """
        classify, records = (
            (classify_other_regime, other_regime) if other_lists else (classify_match, in_scope)
        )
        found: dict[BlockSet, dict[str, list[str]]] = {"BLOCKED": {}, "POSSIBLY_BLOCKED": {}}
        for match in self.matches:
            block_set = classify(self.pack, match)
            if block_set is not None:
                ids = [record.id for record in records(self.pack, match)]
                found[block_set].setdefault(match.entity_id, []).extend(ids)
        blocked = found["BLOCKED"]
        review = {n: ids for n, ids in found["POSSIBLY_BLOCKED"].items() if n not in blocked}
        return blocked, review

    def _other_regime(
        self, in_b: set[str], in_p: set[str]
    ) -> tuple[NodeDerivation | None, list[str]]:
        """Would the designations on every list catch the target where the pack's lists do not?

        The pack's test is run again with the other lists' designations added to
        its own. Returns the target's derivation in that run and the other lists'
        sanction ids, when it puts the target in a stricter set than the pack's
        lists do; otherwise (None, []).
        """
        other_b, other_p = self._seeds(other_lists=True)
        if not other_b and not other_p:
            return None, []
        own_b, own_p = self._seeds()
        seeds_b = {n: own_b.get(n, []) + other_b.get(n, []) for n in own_b.keys() | other_b}
        seeds_p = {
            n: own_p.get(n, []) + other_p.get(n, [])
            for n in (own_p.keys() | other_p) - seeds_b.keys()
        }
        blocked = self._blocked(seeds_b)
        all_b = set(blocked.why)
        possibly = self._possibly_blocked(
            {n: ids for n, ids in seeds_p.items() if n not in all_b}, all_b
        )
        target = self.target
        if target in all_b and target not in in_b:
            why = blocked.why[target]
        elif target in possibly.why and target not in in_b | in_p:
            why = possibly.why[target]
        else:
            return None, []
        cited = sorted({i for ids in (*other_b.values(), *other_p.values()) for i in ids})
        return why, cited

    # --- the fix-points --------------------------------------------------------------------

    def _every(self, stake: Stake) -> bool:
        return stake.every_meets(self.pack.threshold_operator, self.threshold)

    def _some(self, stake: Stake) -> bool:
        return stake.some_meets(self.pack.threshold_operator, self.threshold)

    def _holding(self, node_id: str, holders: set[str]) -> _Holding | None:
        """What `holders` own of the node directly, on the measure that tells most.

        For each measure the pack tests, the holders' stakes are added together
        or the largest is taken, as the pack says. The measure reported is the
        one whose stake meets the test for every value, else for some, else the
        largest; equity before votes on a tie. None when no holder has a stake.
        """
        found = []
        for order, measure in enumerate(self.pack.ownership_measures):
            hops = [hop for hop in self.owners[node_id] if hop.upper in holders]
            held = [(hop, item[0]) for hop in hops if (item := hop.stake(measure)) is not None]
            if not held:
                continue
            stakes = [stake for _, stake in held]
            if self.pack.aggregate_blocked_owners:
                total, used = stake_sum(stakes), [hop for hop, _ in held]
            else:
                total = stake_max(stakes)
                used = [
                    hop for hop, stake in held if stake.low == total.low or stake.high == total.high
                ]
            rank = (self._every(total), self._some(total), total.rank(), -order)
            found.append((rank, _Holding(measure, total, tuple(used))))
        return max(found, key=lambda item: item[0])[1] if found else None

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
                holding = self._holding(node_id, holders)
                if holding is not None and self._every(holding.stake):
                    members.add(node_id, "AGGREGATE", round_, holding.stake, holding.edges)
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
                holding = self._holding(node_id, holders)
                if holding is None:
                    continue
                if self._every(holding.stake):
                    members.add(node_id, "AGGREGATE", round_, holding.stake, holding.edges)
                    added = True
                elif self._some(holding.stake):
                    # Some values of a stated range meet the test and others do not.
                    members.add(node_id, "RANGE", round_, holding.stake, holding.edges)
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
    ) -> tuple[EffectiveExposure, Stake | None, list[_Path]]:
        """Exposure, its exact sum, and the paths it was taken over.

        A path counts from the last blocked entity on it: that entity is blocked
        in full, so what blocked parties above it hold of it adds nothing. A hop
        counts its largest stake on any measure the pack tests.
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
            stakes = [hop.largest for hop in path.hops]
            if all(item is not None for item in stakes):
                values.append(stake_product([item[0] for item in stakes if item is not None]))
        if not values:
            return EffectiveExposure(has_unknown=bool(counted)), None, counted
        total = stake_sum(values)
        ranged = not all(value.is_point for value in values)
        exposure = EffectiveExposure(
            max_pct=_decimal(max(value.low for value in values)),
            sum_pct=_decimal(total.low),
            has_unknown=len(values) < len(counted),
            max_pct_high=_decimal(max(value.high for value in values), up=True) if ranged else None,
            sum_pct_high=_decimal(total.high, up=True) if ranged else None,
        )
        return exposure, total, counted

    # --- gaps ------------------------------------------------------------------------------

    def _gaps(self, paths: list[_Path], blocked: set[str], by_range: set[str]) -> list[DataGap]:
        on_path_nodes = {node_id for path in paths for node_id in path.nodes}
        on_path_edges = {edge_id for path in paths for hop in path.hops for edge_id in hop.edge_ids}
        every = self.pack.gap_scope == "any"
        measures = set(self.pack.ownership_measures)
        gaps: dict[tuple, DataGap] = {}

        def add(code: GapCode, subject_id: str, provenance: Provenance, relevant: bool) -> None:
            gap = DataGap(
                code=code, subject_id=subject_id, decision_relevant=relevant, provenance=provenance
            )
            gaps[gap.sort_key()] = gap

        for edge in self.edges:
            # A control link, or a stake of a measure the pack does not test, has no
            # percentage the pack reads, so its absence is not a gap.
            if edge.rel_type not in OWNERSHIP_RELATIONS or edge.interest not in measures:
                continue
            if not edge.stated:
                add("NULL_PCT_EDGE", edge.id, edge.provenance, every or edge.id in on_path_edges)
            elif edge.is_range:
                # It decides where the range alone makes the node it leads into possibly blocked.
                relevant = every or upper_lower(edge)[1] in by_range
                add("PCT_RANGE", edge.id, edge.provenance, relevant)
        for record in self.subgraph.reporting_exceptions:
            if record.entity_id in self.upward:
                relevant = every or record.entity_id in on_path_nodes
                add("REPORTING_EXCEPTION", record.entity_id, record.provenance, relevant)
        for node_id, node in self.nodes.items():
            if node.lei_status in UNMAINTAINED_LEI_STATUSES:
                add("LAPSED_LEI", node_id, node.provenance, every or node_id in on_path_nodes)
        for match in self.matches:
            in_band = classify_match(self.pack, match) == "POSSIBLY_BLOCKED"
            if in_band and match.entity_id not in blocked:
                record = in_scope(self.pack, match)[0]
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

    def _stake_coverage(self, paths: list[_Path]) -> StakeCoverage:
        """The ownership hops on the paths: with an exact stake, a range, or none."""
        hops = {id(hop): hop for path in paths for hop in path.hops if hop.ownership}
        counts = {"exact": 0, "range": 0, "none": 0}
        for hop in hops.values():
            stakes = [stake for _, stake, _ in hop.stakes]
            if any(stake.is_point for stake in stakes):
                counts["exact"] += 1
            elif stakes:
                counts["range"] += 1
            else:
                counts["none"] += 1
        return StakeCoverage(**counts)

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
        by_range = {n for n, why in possibly.why.items() if why.basis == "RANGE"}

        paths = self._paths(in_b | in_p)
        exposure, exposure_sum, exposure_paths = self._exposure(paths, in_b)
        gaps = self._gaps(paths, in_b, by_range)
        direct = self._holding(target, in_b - {target})

        findings: list[ReasonFinding] = []

        def found(
            code: ReasonCode,
            subject_id: str = target,
            stake: Stake | None = None,
            edges: Iterable[Relationship] = (),
            sanction_ids: Iterable[str] = (),
        ) -> None:
            low, high, bounds = _reported(stake)
            findings.append(
                ReasonFinding(
                    code=code,
                    subject_id=subject_id,
                    pct=low,
                    pct_high=high,
                    pct_bounds=bounds,
                    edge_ids=[edge.id for edge in edges],
                    sanction_ids=list(sanction_ids),
                )
            )

        def derived(code: ReasonCode, why: NodeDerivation, sanction_ids: list[str]) -> None:
            findings.append(
                ReasonFinding(
                    code=code,
                    subject_id=target,
                    pct=why.aggregate_pct,
                    pct_high=why.aggregate_pct_high,
                    pct_bounds=why.pct_bounds,
                    edge_ids=why.edge_ids,
                    sanction_ids=sanction_ids,
                )
            )

        def at_least(stake: Stake, level: Decimal) -> bool:
            # A risk signal fires on the cautious side: some value reaches the level.
            return stake.some_meets(">=", Fraction(level))

        if target in in_b:
            recommendation = "ESCALATE"
            holding = direct
            if target in blocked_seeds:
                found("DIRECT_DESIGNATION", sanction_ids=blocked_seeds[target])
                # Would the target stay blocked if its own designation were lifted?
                others = {n: ids for n, ids in blocked_seeds.items() if n != target}
                without = set(self._blocked(others).why)
                holding = self._holding(target, without - {target}) if target in without else None
            if holding is not None:
                found("DERIVED_50PCT", stake=holding.stake, edges=holding.edges)
        else:
            if target in in_p:
                why = possibly.why[target]
                derived("POSSIBLY_BLOCKED", why, why.sanction_ids)
            # The sum over paths: the largest single path understates a holding
            # that is split across vehicles.
            if exposure_sum is not None and at_least(exposure_sum, pack.review_exposure_pct):
                edges = [hop.edge for path in exposure_paths for hop in path.hops]
                found("EFFECTIVE_EXPOSURE", stake=exposure_sum, edges=edges)
            if direct is not None and at_least(direct.stake, pack.near_miss_pct):
                found("NEAR_MISS_AGGREGATE", stake=direct.stake, edges=direct.edges)
            for subject_id in sorted({gap.subject_id for gap in gaps if gap.decision_relevant}):
                found("DECISION_RELEVANT_GAP", subject_id=subject_id)
            if pack.other_designations == "review":
                why, cited = self._other_regime(in_b, in_p)
                if why is not None:
                    derived("OTHER_REGIME_DESIGNATION", why, cited)
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
                # A blocked party whose path to the target crosses a control link is
                # reviewed, at either scope: a weaker match on the same party already
                # is (a review-band gap on its path), so a stronger one must never
                # clear the target. A path is read from the last blocked or
                # possibly-blocked party on it; one higher up adds nothing.
                flagged = in_b | in_p
                reported = {f.subject_id for f in findings if f.code == "CONTROL_LINK"}
                chains: dict[str, dict[str, Relationship]] = defaultdict(dict)
                for path in paths:
                    if (
                        path.start in flagged
                        and not any(hop.lower in flagged for hop in path.hops[:-1])
                        and any(not hop.ownership for hop in path.hops)
                    ):
                        chains[path.start].update((hop.edge.id, hop.edge) for hop in path.hops)
                for start in sorted(chains.keys() - reported):
                    edges = [chains[start][i] for i in sorted(chains[start])]
                    found("CONTROL_LINK", subject_id=start, edges=edges)
            recommendation = "REVIEW" if findings else "CLEAR"

        seeds = set(blocked_seeds) | set(review_seeds)
        sources = {node.provenance.source for node in self.nodes.values()}
        sources.update(edge.provenance.source for edge in self.edges)
        sources.update(r.provenance.source for m in self.matches for r in m.sanction_records)
        aggregate, aggregate_high, aggregate_bounds = _reported(direct.stake if direct else None)
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
            aggregate_pct=aggregate if aggregate is not None else Decimal(0),
            aggregate_pct_high=aggregate_high,
            aggregate_pct_bounds=aggregate_bounds,
            findings=findings,
            derivations=[*blocked.why.values(), *possibly.why.values()],
            stake_coverage=self._stake_coverage(paths),
            max_depth=self.subgraph.max_depth_up,
            nodes_checked=len(self.nodes),
            edges_checked=len(self.edges),
            sources_checked=sorted(sources),
        )
