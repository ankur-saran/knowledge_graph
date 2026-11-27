"""Measure each name-matching rule alone (Step 19.2). Not shipped.

    uv run python eval/tools/er_rules.py [--snapshot fixtures] [--labels F] [--pairs F]

For each of: no rule, each rule of `name_match.ALL_RULES` alone, and every rule,
it scores the labelled queries (through `resolve()`, on a built set, read-only)
and the labelled pairs, and prints per variant type the figures at the pack's
two operating points (0.85 and 0.92): precision@1 and recall@5 for queries, the
share of rows naming no entity that return a candidate, and for pairs recall and
the share of pairs labelled different that are linked.

The rules switched at query time are the scorer's. The particle-joined blocks of
Gold were built with every rule on, so "no rule" still finds names through them:
the queries' own blocks follow the switch, the graph's do not.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from ubo_sentinel.graph.entity_resolution import (  # noqa: E402
    load_pairs,
    score_labelled,
    score_pairs,
    sweep_labelled,
    sweep_pairs,
)
from ubo_sentinel.graph.store import GraphStore  # noqa: E402
from ubo_sentinel.pipeline import name_match  # noqa: E402

POINTS = (85, 92)


def _pct(value: float | None) -> str:
    return "    -" if value is None else f"{100 * value:5.1f}"


def _figures(store, labels, pairs) -> dict[str, str]:
    """Variant type -> one line of figures at the operating points."""
    lines: dict[str, list[str]] = {}
    if store is not None and labels is not None:
        sweep = sweep_labelled(score_labelled(store, labels), POINTS)
        for kind in sweep[POINTS[0]]:
            for point in POINTS:
                c = sweep[point][kind]
                lines.setdefault(f"query {kind}", []).append(
                    f"@{point} p@1 {_pct(c.precision_at_1)} r@5 {_pct(c.recall_at_5)}"
                    f" found {_pct(c.false_candidate_rate)}"
                )
    if pairs:
        sweep = sweep_pairs(score_pairs(pairs), POINTS)
        for kind in sweep[POINTS[0]]:
            for point in POINTS:
                c = sweep[point][kind]
                different = c.false_pos + c.true_neg
                linked = c.false_pos / different if different else None
                lines.setdefault(f"pair  {kind}", []).append(
                    f"@{point} recall {_pct(c.recall)} fp {_pct(linked)}"
                )
    return {key: " | ".join(cells) for key, cells in sorted(lines.items())}


def measure(store: GraphStore | None, labels: Path | None, pairs: Path | None) -> None:
    """Print the baseline (no rule) in full, then for each rule alone and for every
    rule the variant types whose figures differ from it."""
    loaded = load_pairs(pairs) if pairs else []
    settings = [(rule, frozenset({rule})) for rule in sorted(name_match.ALL_RULES)]
    settings += [("all", name_match.ALL_RULES)]
    try:
        name_match.ENABLED_RULES = frozenset()
        base = _figures(store, labels, loaded)
        print("== no rule")
        for key, line in base.items():
            print(f"  {key:24} {line}")
        for name, rules in settings:
            name_match.ENABLED_RULES = rules
            found = _figures(store, labels, loaded)
            print(f"== {name}: what moved")
            moved = [key for key in found if found[key] != base.get(key)]
            for key in moved:
                print(f"  {key:24} {base.get(key, '-')}")
                print(f"  {'':24} {found[key]}")
            if not moved:
                print("  nothing")
    finally:
        name_match.ENABLED_RULES = name_match.ALL_RULES


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", default="fixtures")
    parser.add_argument("--labels", type=Path, default=Path("eval/er_labelled.csv"))
    parser.add_argument("--pairs", type=Path, default=Path("eval/er_pairs.csv"))
    parser.add_argument("--no-queries", action="store_true", help="Score the pairs only.")
    args = parser.parse_args()
    if args.no_queries:
        measure(None, None, args.pairs)
        return
    from ubo_sentinel.pipeline.db import connect

    con = connect(read_only=True)
    try:
        with GraphStore.open(args.snapshot, con) as store:
            measure(store, args.labels, args.pairs)
    finally:
        con.close()


if __name__ == "__main__":
    main()
