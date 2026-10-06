"""Rule Engine Agent: runs the rule pack, and says what would settle a REVIEW."""

from ubo_sentinel.agents.base import BaseAgent, PipelineState, ScreenContext
from ubo_sentinel.models.decision import Recommendation
from ubo_sentinel.models.graph import SanctionMatch, SubGraph
from ubo_sentinel.models.memo import ContestedMatch, WhatIf
from ubo_sentinel.models.rule_result import RuleResult
from ubo_sentinel.rules.engine import PathLimitExceeded, RuleEngine, classify_match


def what_if(
    engine: RuleEngine, subgraph: SubGraph, matches: list[SanctionMatch], result: RuleResult
) -> WhatIf | None:
    """The recommendation with every unsettled match confirmed, and with each rejected.

    Unsettled: a designation that reached its node across a link the rules do
    not take as proof (a name in the review band, a contested identifier). The
    rules stay as they are; this only tells a reviewer what the review decides,
    and when it decides nothing.
    """
    if result.recommendation != "REVIEW":
        return None
    upward = set(subgraph.depth_up)
    contested = [
        match
        for match in matches
        if match.entity_id in upward
        and match.entity_id not in result.blocked_set
        and classify_match(engine.pack, match) == "POSSIBLY_BLOCKED"
    ]
    if not contested:
        return None
    settled = [match for match in matches if match not in contested]
    # Confirmed, the designation is the node's own.
    confirmed = [
        match.model_copy(update={"designated_entity_id": match.entity_id}) for match in contested
    ]

    def recommend(with_matches: list[SanctionMatch]) -> Recommendation:
        try:
            return engine.evaluate(subgraph, with_matches).recommendation
        except PathLimitExceeded:
            return "REVIEW"

    return WhatIf(
        contested=[
            ContestedMatch(entity_id=m.entity_id, designated_entity_id=m.designated_entity_id)
            for m in contested
        ],
        if_confirmed=recommend(settled + confirmed),
        if_rejected=recommend(settled),
    )


class RuleEngineAgent(BaseAgent):
    name = "RuleEngine"
    requires = ("subgraph", "sanction_matches")
    provides = ("rule_result",)

    def run(self, state: PipelineState, ctx: ScreenContext) -> PipelineState:
        try:
            result = ctx.engine.evaluate(state.subgraph, state.sanction_matches)
        except PathLimitExceeded:
            # More evidence paths than one screen may enumerate: nothing was
            # concluded, and that is a reason to review, not an error.
            return state.with_(halt="EVIDENCE_LIMIT_EXCEEDED")
        return state.with_(
            rule_result=result,
            what_if=what_if(ctx.engine, state.subgraph, state.sanction_matches, result),
        )
