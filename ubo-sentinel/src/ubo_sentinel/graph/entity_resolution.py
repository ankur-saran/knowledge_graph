"""Query-time entity resolution: which entities a name or identifier may mean.

Identifiers come first: an LEI, then a registration number. A name is matched
exactly in its normalised form, then by similarity within its blocks, with the
scorer Silver links entities with. Normalising drops legal forms, so names of
one company in many countries tie ("Siemens AG", "Siemens Limited", "Siemens
SAS"); a tie goes to the jurisdiction asked for, then to the name whose legal
form is the query's (`form_similarity`), then to the smaller id.
"""

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ubo_sentinel.graph.store import GraphStore, id_list
from ubo_sentinel.models.graph import CandidateLink, EntityMatch
from ubo_sentinel.pipeline.entity_linking import BLOCK_PREFIX_LENGTH
from ubo_sentinel.pipeline.jurisdiction import normalise_jurisdiction
from ubo_sentinel.pipeline.name_match import (
    form_similarity,
    name_similarity,
    normalise_name,
    similarity_normalised,
)

# Lowest name score that is returned as a candidate. Below Silver's link floor
# (`LINK_FLOOR`): a candidate is shown to a person, who chooses, so a typo is
# better found than missed; a link carries a designation unseen, so it is held
# to more.
RESOLVE_FLOOR = 0.75

# Names scored for one query at most. Blocks are read from the rarest to the
# most common until this many names are in hand.
CANDIDATE_CAP = 5000

_LEI = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")

_NODE_COLUMNS = "id, legal_name, jurisdiction, entity_type, status, lei, source"


def resolve(
    store: GraphStore,
    query: str,
    lei: str | None = None,
    registration_number: str | None = None,
    jurisdiction: str | None = None,
    max_candidates: int = 10,
) -> list[EntityMatch]:
    """Candidates for a query, best first.

    An identifier that matches decides alone. Otherwise candidates are names
    scoring at least `RESOLVE_FLOOR`, ordered by score, then by whether the
    jurisdiction is the one asked for, then by how closely the name with its
    legal form matches the query's, then by id. Jurisdiction never filters.
    """
    lei = lei or (query.strip().upper() if _LEI.match(query.strip().upper()) else None)
    jurisdiction = normalise_jurisdiction(jurisdiction)
    if lei:
        found = _by_identifier(store, "lei", lei, "LEI")
        if found:
            return _with_links(store, found[:max_candidates])
    if registration_number:
        found = _by_identifier(store, "registration_number", registration_number, "REGISTRATION")
        if found:
            return _with_links(store, found[:max_candidates])

    scored = _score_names(store, query)
    if not scored:
        return []
    nodes = _nodes(store, sorted(scored))
    matches = [
        EntityMatch(
            entity_id=node_id,
            confidence=score,
            match_type="NAME_EXACT" if exact else "NAME_FUZZY",
            matched_name=name,
            **_display(nodes[node_id]),
        )
        for node_id, (score, exact, _, name) in scored.items()
    ]

    def rank(match: EntityMatch) -> tuple[float, bool, float, str]:
        elsewhere = jurisdiction is not None and match.jurisdiction != jurisdiction
        form = scored[match.entity_id][2]
        return (-match.confidence, elsewhere, -form, match.entity_id)

    matches.sort(key=rank)
    return _with_links(store, matches[:max_candidates])


def _with_links(store: GraphStore, matches: list[EntityMatch]) -> list[EntityMatch]:
    """Say which of the candidates are linked to each other, and how closely."""
    ids = [match.entity_id for match in matches]
    if len(ids) < 2:
        return matches
    rows = store.con.execute(
        "SELECT node_id, linked_node_id, confidence FROM graph_links"
        f" WHERE snapshot_set_id = ? AND node_id IN {id_list()} AND linked_node_id IN {id_list()}",
        [store.set_id, ids, ids],
    ).fetchall()
    linked: dict[str, list[CandidateLink]] = {}
    for node_id, linked_id, confidence in rows:
        linked.setdefault(node_id, []).append(
            CandidateLink(entity_id=linked_id, confidence=confidence)
        )
    return [
        match.model_copy(update={"linked": sorted(linked[match.entity_id], key=_link_key)})
        if match.entity_id in linked
        else match
        for match in matches
    ]


def _link_key(link: CandidateLink) -> str:
    return link.entity_id


def _display(node: dict[str, Any]) -> dict[str, Any]:
    keys = ("legal_name", "jurisdiction", "entity_type", "status", "lei", "source")
    return {key: node[key] for key in keys}


def _nodes(store: GraphStore, ids: list[str]) -> dict[str, dict[str, Any]]:
    rows = store.rows(
        f"SELECT {_NODE_COLUMNS} FROM graph_nodes WHERE snapshot_set_id = ? AND id IN {id_list()}",
        [store.set_id, ids],
    )
    return {row["id"]: row for row in rows}


def _by_identifier(
    store: GraphStore, column: str, value: str, match_type: str
) -> list[EntityMatch]:
    rows = store.rows(
        f"SELECT {_NODE_COLUMNS} FROM graph_nodes"
        f" WHERE snapshot_set_id = ? AND {column} = ? ORDER BY id",
        [store.set_id, value],
    )
    return [
        EntityMatch(
            entity_id=row["id"],
            confidence=1.0,
            match_type=match_type,
            matched_name=row["legal_name"],
            **_display(row),
        )
        for row in rows
    ]


def _score_names(store: GraphStore, query: str) -> dict[str, tuple[float, bool, float, str]]:
    """Node id -> (best score, whether a name is the query exactly, how closely that
    name with its legal form matches the query's, that name)."""
    query_norm = normalise_name(query)
    blocks = sorted({token[:BLOCK_PREFIX_LENGTH] for token in query_norm.split()})
    if not blocks:
        return {}
    # A name equal to the query is found whatever the size of its blocks.
    names = store.con.execute(
        "SELECT DISTINCT node_id, name, name_norm FROM graph_name_blocks"
        f" WHERE snapshot_set_id = ? AND block IN {id_list()} AND name_norm = ?",
        [store.set_id, blocks, query_norm],
    ).fetchall()
    sizes = store.con.execute(
        "SELECT block, count(*) FROM graph_name_blocks"
        f" WHERE snapshot_set_id = ? AND block IN {id_list()} GROUP BY block ORDER BY 2, 1",
        [store.set_id, blocks],
    ).fetchall()
    in_hand = 0
    for block, size in sizes:
        if in_hand and in_hand + size > CANDIDATE_CAP:
            break
        names += store.con.execute(
            "SELECT node_id, name, name_norm FROM graph_name_blocks"
            " WHERE snapshot_set_id = ? AND block = ?",
            [store.set_id, block],
        ).fetchall()
        in_hand += size

    best: dict[str, tuple[float, bool, float, str]] = {}
    for node_id, name, name_norm in sorted(set(names)):
        score = similarity_normalised(query_norm, name_norm)
        if score < RESOLVE_FLOOR:
            continue
        found = (score, name_norm == query_norm, form_similarity(query, name), name)
        # The higher score wins, then the closer legal form; then the first name in order.
        if node_id not in best or found[:3] > best[node_id][:3]:
            best[node_id] = found
    return best


@dataclass(frozen=True)
class ErReport:
    """Precision@1 over a labelled set of queries."""

    # Rows that name an entity, and how many of them resolved to it first.
    total: int
    correct: int
    # (query, expected id, id returned first or None).
    misses: list[tuple[str, str, str | None]]
    # `negative` rows (invented names) that returned a candidate: (query, id, score).
    false_candidates: list[tuple[str, str, float]]
    # Other rows that name no entity (`near_homonym`: a real name one letter
    # away) and returned a candidate. Measured, not held to nothing (Step 18).
    homonym_candidates: list[tuple[str, str, float]] = field(default_factory=list)

    @property
    def precision_at_1(self) -> float:
        return self.correct / self.total if self.total else 1.0


# The labelled sets' variant type for invented names, which must find nothing.
NEGATIVE = "negative"


@dataclass(frozen=True)
class ScoredRow:
    """One labelled query, resolved once: what the sweep cuts at every threshold."""

    query: str
    # The entity the row names; None for a row that names none.
    expected: str | None
    variant_type: str
    # (entity id, score), best first, as `resolve()` returned them.
    candidates: tuple[tuple[str, float], ...]


def score_labelled(store: GraphStore, path: Path) -> list[ScoredRow]:
    """Resolve every row of a labelled set (`eval/er_labelled.csv`) once.

    A row with a jurisdiction is resolved with it, which is what tells two
    homonyms apart. A row with no `expected_entity_id` names no entity.
    Columns other than the five read here (`labelled_by`, `set_id`) say who
    labelled the row and on which set.
    """
    rows: list[ScoredRow] = []
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            found = resolve(
                store,
                row["query_name"],
                lei=row["lei"] or None,
                jurisdiction=row["jurisdiction"] or None,
            )
            rows.append(
                ScoredRow(
                    query=row["query_name"],
                    expected=row["expected_entity_id"] or None,
                    variant_type=row["variant_type"],
                    candidates=tuple((match.entity_id, match.confidence) for match in found),
                )
            )
    return rows


def report_of(rows: list[ScoredRow]) -> ErReport:
    """Precision@1 at the resolution floor.

    A row is correct when the entity it names comes first. A `negative` row must
    return nothing; another row that names no entity is counted when it does.
    """
    total = correct = 0
    misses: list[tuple[str, str, str | None]] = []
    false_candidates: list[tuple[str, str, float]] = []
    homonym_candidates: list[tuple[str, str, float]] = []
    for row in rows:
        if row.expected is None:
            found = [(row.query, entity_id, score) for entity_id, score in row.candidates]
            if row.variant_type == NEGATIVE:
                false_candidates += found
            else:
                homonym_candidates += found
            continue
        total += 1
        first = row.candidates[0][0] if row.candidates else None
        if first == row.expected:
            correct += 1
        else:
            misses.append((row.query, row.expected, first))
    return ErReport(total, correct, misses, false_candidates, homonym_candidates)


def evaluate_labelled(store: GraphStore, path: Path) -> ErReport:
    """Resolve every row of a labelled set and score it at the resolution floor."""
    return report_of(score_labelled(store, path))


# The sweep's cut-offs, in hundredths: 0.75 to 1.00 by 0.01 (Step 18).
SWEEP_THRESHOLDS = tuple(range(75, 101))
# Every variant type together.
ALL_TYPES = "all"


def cut(score: float, threshold: int) -> bool:
    """Whether a score is at or above a cut-off in hundredths, compared as code compares
    it with a pack's threshold (`0.85` is `85 / 100`)."""
    return score >= threshold / 100


@dataclass(frozen=True)
class ResolutionCounts:
    """Labelled queries of one variant type at one cut-off."""

    # Rows that name an entity; of them, found first and found in the first five.
    named: int = 0
    first: int = 0
    in_top_5: int = 0
    # Rows that name none; of them, those that returned a candidate.
    unnamed: int = 0
    with_candidate: int = 0

    @property
    def precision_at_1(self) -> float | None:
        return self.first / self.named if self.named else None

    @property
    def recall_at_5(self) -> float | None:
        return self.in_top_5 / self.named if self.named else None

    @property
    def false_candidate_rate(self) -> float | None:
        return self.with_candidate / self.unnamed if self.unnamed else None


def sweep_labelled(
    rows: list[ScoredRow], thresholds: tuple[int, ...] = SWEEP_THRESHOLDS
) -> dict[int, dict[str, ResolutionCounts]]:
    """Cut-off -> variant type (and `all`) -> counts, from the stored scores.

    Candidates come best first, so those at or above a cut-off are a prefix of
    what `resolve()` returned: the first of them is the first at that cut-off.
    """
    sweep: dict[int, dict[str, ResolutionCounts]] = {}
    for threshold in thresholds:
        counts: dict[str, list[int]] = {}
        for row in rows:
            kept = [entity_id for entity_id, score in row.candidates if cut(score, threshold)]
            for key in (row.variant_type, ALL_TYPES):
                c = counts.setdefault(key, [0, 0, 0, 0, 0])
                if row.expected is None:
                    c[3] += 1
                    c[4] += bool(kept)
                else:
                    c[0] += 1
                    c[1] += bool(kept) and kept[0] == row.expected
                    c[2] += row.expected in kept[:5]
        sweep[threshold] = {key: ResolutionCounts(*c) for key, c in sorted(counts.items())}
    return sweep


@dataclass(frozen=True)
class LabelledPair:
    """Two names labelled one party or two (`eval/er_pairs_real.csv`), for linking."""

    name_a: str
    name_b: str
    entity_type: str
    same: bool
    variant_type: str
    # As the source states them, or empty: Step 19 reads them, the score does not yet.
    birth_year_a: str = ""
    nationality_a: str = ""
    birth_year_b: str = ""
    nationality_b: str = ""


PAIR_COLUMNS = (
    "name_a", "name_b", "entity_type", "same", "variant_type",
    "birth_year_a", "nationality_a", "birth_year_b", "nationality_b",
    "labelled_by", "set_id",
)  # fmt: skip


def load_pairs(path: Path) -> list[LabelledPair]:
    """Read a labelled pair file; `same` is `1` or `0`. The five columns scored are
    required; the person attributes and who labelled the pair on which set may be absent."""
    pairs: list[LabelledPair] = []
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in PAIR_COLUMNS[:5] if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path.as_posix()} lacks columns {', '.join(missing)}")
        for line, row in enumerate(reader, start=2):
            if row["same"] not in ("0", "1"):
                raise ValueError(f"{path.as_posix()}:{line}: same is {row['same']!r}, not 0 or 1")
            pairs.append(
                LabelledPair(
                    name_a=row["name_a"],
                    name_b=row["name_b"],
                    entity_type=row["entity_type"],
                    same=row["same"] == "1",
                    variant_type=row["variant_type"],
                    birth_year_a=row.get("birth_year_a") or "",
                    nationality_a=row.get("nationality_a") or "",
                    birth_year_b=row.get("birth_year_b") or "",
                    nationality_b=row.get("nationality_b") or "",
                )
            )
    return pairs


@dataclass(frozen=True)
class PairCounts:
    """Labelled pairs of one variant type at one cut-off: a pair at or above it is linked."""

    true_pos: int = 0
    false_pos: int = 0
    false_neg: int = 0
    true_neg: int = 0

    @property
    def precision(self) -> float | None:
        linked = self.true_pos + self.false_pos
        return self.true_pos / linked if linked else None

    @property
    def recall(self) -> float | None:
        same = self.true_pos + self.false_neg
        return self.true_pos / same if same else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None:
            return None
        return 2 * p * r / (p + r) if p + r else 0.0


def score_pairs(pairs: list[LabelledPair]) -> list[tuple[LabelledPair, float]]:
    """Each pair with the score Silver links by (`name_similarity()`), computed once."""
    return [(pair, name_similarity(pair.name_a, pair.name_b)) for pair in pairs]


def sweep_pairs(
    scored: list[tuple[LabelledPair, float]], thresholds: tuple[int, ...] = SWEEP_THRESHOLDS
) -> dict[int, dict[str, PairCounts]]:
    """Cut-off -> variant type (and `all`) -> counts, from the stored scores."""
    sweep: dict[int, dict[str, PairCounts]] = {}
    for threshold in thresholds:
        counts: dict[str, list[int]] = {}
        for pair, score in scored:
            linked = cut(score, threshold)
            slot = (0 if linked else 2) if pair.same else (1 if linked else 3)
            for key in (pair.variant_type, ALL_TYPES):
                counts.setdefault(key, [0, 0, 0, 0])[slot] += 1
        sweep[threshold] = {key: PairCounts(*c) for key, c in sorted(counts.items())}
    return sweep
