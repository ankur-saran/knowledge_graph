"""Intake Agent: which entity the query means.

It selects a target on its own only when the answer is not in doubt: one
identifier match, or one name at or above the rule pack's `match_threshold`.
Anything else goes to the analyst. A query that matches nothing is never CLEAR.
"""

from ubo_sentinel.agents.base import (
    BaseAgent,
    PipelineState,
    Resolution,
    ScreenContext,
    ScreenUsageError,
)
from ubo_sentinel.graph import queries
from ubo_sentinel.models.graph import EntityMatch
from ubo_sentinel.pipeline.sources import SOURCE_RANK

IDENTIFIER_MATCHES = frozenset({"LEI", "REGISTRATION"})


def fold_linked(
    candidates: list[EntityMatch], match_threshold: float
) -> tuple[list[EntityMatch], dict[str, list[str]]]:
    """One candidate per entity: records linked at or above the threshold are one choice.

    A registry and a list each hold a record of a listed company, and the two
    are linked by name, never merged. Asking an analyst to choose between them
    is asking the wrong question. The registry's record stands for the group
    (it carries the ownership the registry reports); the rule engine brings the
    other record's designations across the link and flags its owners.

    Returns the candidates in their order, and candidate -> the records folded into it.
    """
    by_id = {match.entity_id: match for match in candidates}
    group = {match.entity_id: match.entity_id for match in candidates}

    def find(entity_id: str) -> str:
        while group[entity_id] != entity_id:
            entity_id = group[entity_id]
        return entity_id

    for match in candidates:
        for link in match.linked:
            if link.confidence >= match_threshold and link.entity_id in group:
                a, b = find(match.entity_id), find(link.entity_id)
                group[max(a, b)] = min(a, b)

    members: dict[str, list[str]] = {}
    for match in candidates:
        members.setdefault(find(match.entity_id), []).append(match.entity_id)

    def rank(entity_id: str) -> tuple[int, str]:
        return (SOURCE_RANK.get(by_id[entity_id].source, len(SOURCE_RANK)), entity_id)

    kept: list[EntityMatch] = []
    folded: dict[str, list[str]] = {}
    placed: set[str] = set()
    for match in candidates:
        root = find(match.entity_id)
        if root in placed:
            continue
        placed.add(root)
        chosen = min(members[root], key=rank)
        kept.append(by_id[chosen])
        others = sorted(set(members[root]) - {chosen})
        if others:
            folded[chosen] = others
    return kept, folded


class IntakeAgent(BaseAgent):
    name = "Intake"
    provides = ("resolution", "target_id")

    def run(self, state: PipelineState, ctx: ScreenContext) -> PipelineState:
        request = state.request
        if request.target_id is not None:
            if queries.get_entity_by_id(ctx.store, request.target_id) is None:
                raise ScreenUsageError(
                    f"No entity '{request.target_id}' in snapshot set {ctx.store.set_id}."
                )
            resolution = Resolution(method="TARGET_ID", target_id=request.target_id)
            return state.with_(resolution=resolution, target_id=request.target_id)

        found = queries.resolve_entity(
            ctx.store,
            request.query,
            lei=request.lei,
            registration_number=request.registration_number,
            jurisdiction=request.jurisdiction,
        )
        candidates, folded = fold_linked(found, ctx.pack.match_threshold)
        if not candidates:
            return state.with_(resolution=Resolution(method="NOT_FOUND"), halt="ENTITY_NOT_FOUND")

        def chosen(method: str, match: EntityMatch) -> PipelineState:
            resolution = Resolution(
                method=method, target_id=match.entity_id, candidates=candidates, folded=folded
            )
            return state.with_(resolution=resolution, target_id=match.entity_id)

        if request.pick is not None:
            if request.pick > len(candidates):
                raise ScreenUsageError(
                    f"There is no candidate {request.pick}: the query has {len(candidates)}."
                )
            return chosen("ANALYST_PICK", candidates[request.pick - 1])

        by_identifier = all(match.match_type in IDENTIFIER_MATCHES for match in candidates)
        if by_identifier and len(candidates) == 1:
            return chosen("IDENTIFIER", candidates[0])
        strong = [match for match in candidates if match.confidence >= ctx.pack.match_threshold]
        if not by_identifier and len(strong) == 1:
            return chosen("SOLE_NAME", strong[0])
        return state.with_(
            resolution=Resolution(method="ANALYST_PICK", candidates=candidates, folded=folded),
            halt="NEEDS_DISAMBIGUATION",
        )
