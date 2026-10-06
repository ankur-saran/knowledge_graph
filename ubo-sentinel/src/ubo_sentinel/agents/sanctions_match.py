"""Sanctions Match Agent: the designations that reach a node of the subgraph.

The matches were computed in Silver (by identifier, and across name links).
Which of them block is for the rule engine to say.
"""

from ubo_sentinel.agents.base import BaseAgent, PipelineState, ScreenContext
from ubo_sentinel.graph import queries


class SanctionsMatchAgent(BaseAgent):
    name = "SanctionsMatch"
    requires = ("subgraph",)
    provides = ("sanction_matches",)

    def run(self, state: PipelineState, ctx: ScreenContext) -> PipelineState:
        matches = queries.get_sanctioned_nodes(ctx.store, state.subgraph)
        return state.with_(sanction_matches=matches)
