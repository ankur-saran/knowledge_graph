"""Traversal Agent: the ownership graph around the target."""

import logging

import networkx as nx

from ubo_sentinel.agents.base import BaseAgent, PipelineState, ScreenContext
from ubo_sentinel.graph import queries

log = logging.getLogger(__name__)


def screen_depth(state: PipelineState, ctx: ScreenContext) -> int:
    """How many hops up this screen goes: the request's, else the rule pack's."""
    return state.request.max_depth or ctx.pack.max_depth


class TraversalAgent(BaseAgent):
    name = "Traversal"
    requires = ("target_id",)
    provides = ("subgraph",)

    def run(self, state: PipelineState, ctx: ScreenContext) -> PipelineState:
        subgraph = queries.get_ownership_subgraph(
            ctx.store, state.target_id, screen_depth(state, ctx), ctx.pack.max_depth_down
        )
        # A cycle is a group of entities that own each other. Counting the
        # groups is linear; listing every cycle is not.
        components = nx.strongly_connected_components(subgraph.to_networkx())
        log.info(
            "traversal: %d hops, %d nodes, %d edges, %d cycles, truncated at %d",
            max(subgraph.depth_up.values()),
            len(subgraph.nodes),
            len(subgraph.edges),
            sum(len(component) > 1 for component in components),
            len(subgraph.truncated_at),
        )
        return state.with_(subgraph=subgraph)
