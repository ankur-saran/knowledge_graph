"""Query-time entity resolution: which entities a name or identifier may mean.

Identifiers come first: an LEI, then a registration number. A name is matched
exactly in its normalised form, then by similarity within its blocks, with the
scorer Silver links entities with.
"""

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ubo_sentinel.graph.store import GraphStore, id_list
from ubo_sentinel.models.graph import EntityMatch
from ubo_sentinel.pipeline.entity_linking import BLOCK_PREFIX_LENGTH, LINK_FLOOR
from ubo_sentinel.pipeline.name_match import normalise_name, similarity_normalised

# Lowest name score that is returned as a candidate. A rule pack's review band
# may not start below it.
RESOLVE_FLOOR = LINK_FLOOR

# Names scored for one query at most. Blocks are read from the rarest to the
# most common until this many names are in hand.
CANDIDATE_CAP = 5000

_LEI = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")

_NODE_COLUMNS = "id, legal_name, jurisdiction, entity_type, status, lei"


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
    jurisdiction is the one asked for, then by id. Jurisdiction never filters.
    """
    lei = lei or (query.strip().upper() if _LEI.match(query.strip().upper()) else None)
    if lei:
        found = _by_identifier(store, "lei", lei, "LEI")
        if found:
            return found[:max_candidates]
    if registration_number:
        found = _by_identifier(store, "registration_number", registration_number, "REGISTRATION")
        if found:
            return found[:max_candidates]

    scored = _score_names(store, normalise_name(query))
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
        for node_id, (score, exact, name) in scored.items()
    ]

    def rank(match: EntityMatch) -> tuple[float, bool, str]:
        elsewhere = jurisdiction is not None and match.jurisdiction != jurisdiction
        return (-match.confidence, elsewhere, match.entity_id)

    matches.sort(key=rank)
    return matches[:max_candidates]


def _display(node: dict[str, Any]) -> dict[str, Any]:
    return {
        key: node[key] for key in ("legal_name", "jurisdiction", "entity_type", "status", "lei")
    }


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


def _score_names(store: GraphStore, query_norm: str) -> dict[str, tuple[float, bool, str]]:
    """Node id -> (best score, whether a name is the query exactly, that name)."""
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

    best: dict[str, tuple[float, bool, str]] = {}
    for node_id, name, name_norm in sorted(set(names)):
        score = similarity_normalised(query_norm, name_norm)
        if score < RESOLVE_FLOOR:
            continue
        found = (score, name_norm == query_norm, name)
        # The higher score wins; between equal scores, the first name in order.
        if node_id not in best or found[:2] > best[node_id][:2]:
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
    # `negative` rows that returned a candidate: (query, id, score).
    false_candidates: list[tuple[str, str, float]]

    @property
    def precision_at_1(self) -> float:
        return self.correct / self.total if self.total else 1.0


def evaluate_labelled(store: GraphStore, path: Path) -> ErReport:
    """Resolve every row of a labelled set (`eval/er_labelled.csv`).

    A row is correct when the entity it names comes first. A row with a
    jurisdiction is resolved with it, which is what tells two homonyms apart.
    A `negative` row must return nothing.
    """
    total = correct = 0
    misses: list[tuple[str, str, str | None]] = []
    false_candidates: list[tuple[str, str, float]] = []
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            found = resolve(
                store,
                row["query_name"],
                lei=row["lei"] or None,
                jurisdiction=row["jurisdiction"] or None,
            )
            if row["variant_type"] == "negative":
                false_candidates += [
                    (row["query_name"], match.entity_id, match.confidence) for match in found
                ]
                continue
            total += 1
            first = found[0].entity_id if found else None
            if first == row["expected_entity_id"]:
                correct += 1
            else:
                misses.append((row["query_name"], row["expected_entity_id"], first))
    return ErReport(total, correct, misses, false_candidates)
