"""Which source records are one entity, and which entities only resemble each other.

Records that share an identifier merge into one entity. Entities whose names
are similar are never merged: they get a link with a confidence, and a
designation reaches the linked entity at that confidence.
"""

from collections.abc import Iterable

# Lowest name score that is stored as a link. Silver is built per snapshot set,
# not per rule pack, so it stores links and the rule engine classifies them; a
# rule pack's `review_band_low` may not be below this.
LINK_FLOOR = 0.75

# Names are compared only when they share the first letters of some word.
BLOCK_PREFIX_LENGTH = 4

KEY_LEI = "LEI"
KEY_REGISTRATION = "REGISTRATION"


def resolve_groups(
    shared_keys: Iterable[tuple[str, str, list[str]]],
    attributes: dict[str, tuple[str | None, str]],
) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """Group the records that share an identifier.

    `shared_keys` is (kind, key, refs holding it), LEI keys first, in a fixed
    order. `attributes` gives each ref's (lei, entity_type).

    Returns ref -> the smallest ref of its group, and the joins that were
    refused as (ref_a, ref_b, kind). A shared LEI always merges. A shared
    registration is refused when the merged group would hold two LEIs or two
    entity types: moving a designation onto another company is worse than
    keeping two records apart.
    """
    parent: dict[str, str] = {}
    leis: dict[str, set[str]] = {}
    types: dict[str, set[str]] = {}

    def find(ref: str) -> str:
        if ref not in parent:
            lei, entity_type = attributes[ref]
            parent[ref] = ref
            leis[ref] = {lei} if lei else set()
            types[ref] = {entity_type}
        while parent[ref] != ref:
            parent[ref] = parent[parent[ref]]
            ref = parent[ref]
        return ref

    refused: list[tuple[str, str, str]] = []
    for kind, _, refs in shared_keys:
        first, *others = sorted(refs)
        for ref in others:
            a, b = find(first), find(ref)
            if a == b:
                continue
            merged_leis, merged_types = leis[a] | leis[b], types[a] | types[b]
            if kind == KEY_REGISTRATION and (len(merged_leis) > 1 or len(merged_types) > 1):
                refused.append((first, ref, kind))
                continue
            root, child = min(a, b), max(a, b)
            parent[child] = root
            leis[root], types[root] = merged_leis, merged_types

    return {ref: find(ref) for ref in sorted(parent)}, refused
