"""The evidence graph of a decision, drawn from its memo.

The graph is the memo's, not the database's: what a reviewer looks at is the
evidence the rules read when the decision was made, whatever has been ingested
or rebuilt since, and a decision from the log is drawn without opening a store.
It is drawn from the role's `DecisionView`, so a masked person is a pseudonym
here as everywhere else.

`evidence_graph()` decides what is drawn and how; `to_html()` writes it out with
the vis-network script that ships with pyvis, inline, so the page loads nothing
from the network. Names come from external lists: the data is embedded as JSON
that cannot close the script element, and the popups show it as text.
"""

import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from importlib import resources
from typing import Literal

from ubo_sentinel.models.memo import DecisionView, Memo

Scope = Literal["paths", "checked"]
NodeState = Literal["designated", "blocked", "possibly_blocked", "other_regime", "outside", "other"]

# Okabe-Ito: told apart with any colour vision. Each state also has its own
# shape and its own word in the label, so colour is never the only sign.
STATE_STYLE: dict[str, tuple[str, str, str]] = {
    # state: (fill, shape, label word)
    "designated": ("#D55E00", "diamond", "DESIGNATED"),
    "blocked": ("#D55E00", "dot", "BLOCKED"),
    "possibly_blocked": ("#E69F00", "triangle", "POSSIBLY BLOCKED"),
    "other_regime": ("#CC79A7", "square", "OTHER LIST"),
    "outside": ("#FFFFFF", "box", "OUTSIDE THE GRAPH"),
    "other": ("#009E73", "dot", ""),
}
EDGE_DASHES = {"OWNS": False, "CONSOLIDATED_BY": [10, 6], "CONTROLS": [2, 4]}
EDGE_COLOUR = "#555555"
ULTIMATE_COLOUR = "#AAAAAA"
LINK_COLOUR = "#0072B2"
DEFAULT_CAP = 300
MAX_LABEL = 40


@dataclass(frozen=True)
class GraphNode:
    id: str
    label: str
    title: str
    state: NodeState
    level: int
    is_target: bool = False
    contested: bool = False
    truncated: bool = False
    gaps: tuple[str, ...] = ()


@dataclass(frozen=True)
class GraphEdge:
    id: str
    upper_id: str
    lower_id: str
    kind: str  # a relation, or MATCH / LINKED_RECORD for what is not an edge of the graph
    label: str
    title: str
    on_path: bool = False
    is_ultimate: bool = False
    gaps: tuple[str, ...] = ()


@dataclass(frozen=True)
class GraphSpec:
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    # Nodes the cap left out.
    not_drawn: int = 0
    scope: Scope = "paths"
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        """Every word the drawing shows, for the render guardrail."""
        return json.dumps([asdict(node) for node in self.nodes] + [asdict(e) for e in self.edges])


def _pct(value: Decimal | None) -> str:
    return "?" if value is None else f"{value.normalize():f}%"


def _short(name: str) -> str:
    return name if len(name) <= MAX_LABEL else name[: MAX_LABEL - 1] + "…"


def _states(memo: Memo) -> dict[str, NodeState]:
    effects: dict[str, set[str]] = {}
    for match in memo.matches:
        effects.setdefault(match.entity_id, set()).add(match.effect)
    states: dict[str, NodeState] = {}
    for entity in memo.entities:
        found = effects.get(entity.id, set())
        if entity.id in memo.blocked_set:
            states[entity.id] = "designated" if "BLOCKED" in found else "blocked"
        elif entity.id in memo.possibly_blocked_set:
            states[entity.id] = "possibly_blocked"
        elif "OTHER_REGIME" in found:
            states[entity.id] = "other_regime"
        elif entity.depth is None:
            states[entity.id] = "outside"
        else:
            states[entity.id] = "other"
    return states


def _with_chains(chosen: set[str], memo: Memo) -> set[str]:
    """The chosen nodes and, for each, the shortest chain of edges down to the target.

    A gap or a designation six hops up is drawn where it sits, not floating.
    """
    child_of: dict[str, str] = {}
    frontier = [memo.target_id]
    while frontier:
        reached = []
        for edge in sorted(memo.edges, key=lambda e: (e.upper_id, e.id)):
            if edge.lower_id in frontier and edge.upper_id not in child_of:
                if edge.upper_id != memo.target_id:
                    child_of[edge.upper_id] = edge.lower_id
                    reached.append(edge.upper_id)
        frontier = reached
    connected = set(chosen)
    for node in chosen:
        while node in child_of:
            node = child_of[node]
            connected.add(node)
    return connected


def evidence_graph(view: DecisionView, scope: Scope = "paths", cap: int = DEFAULT_CAP) -> GraphSpec:
    """What to draw of a decision's evidence, in a fixed order.

    `paths` draws the target, the evidence paths, the nodes and edges a gap is
    about and what a designation or a duplicate links to them; `checked` draws
    every edge the screen checked. Either way, nodes beyond `cap` are counted,
    not drawn, and the nodes the decision rests on are drawn first.
    """
    memo = view.memo
    if memo.target_id is None:
        return GraphSpec([], [], scope=scope, notes=["The query matched no entity: no graph."])
    entities = {entity.id: entity for entity in memo.entities}
    states = _states(memo)
    path_nodes = {node for path in memo.paths for node in path.node_ids}
    path_edges = {edge for path in memo.paths for edge in path.edge_ids}
    gaps: dict[str, list[str]] = {}
    for gap in view.data_gaps:
        gaps.setdefault(gap.subject_id, []).append(gap.code)
    contested = {item.entity_id for item in memo.what_if.contested} if memo.what_if else set()
    truncated = set(memo.coverage.truncated_at)

    # What is not an edge of the graph: a designation that crossed a link, and a
    # duplicate record whose owners the screened graph does not show.
    extra: list[GraphEdge] = []
    for match in memo.matches:
        if match.designated_entity_id != match.entity_id and match.designated_entity_id in entities:
            extra.append(
                GraphEdge(
                    id=f"match:{match.designated_entity_id}:{match.entity_id}",
                    upper_id=match.designated_entity_id,
                    lower_id=match.entity_id,
                    kind="MATCH",
                    label=f"{match.match_type} {match.confidence:.2f}",
                    title=f"Designation reaches this record by {match.match_type}"
                    f" ({match.confidence:.2f}); effect {match.effect}",
                )
            )
    for action in memo.next_actions:
        linked = action.params.get("linked_id")
        if action.code == "SCREEN_LINKED_RECORD" and isinstance(linked, str) and action.subject_id:
            if linked in entities:
                extra.append(
                    GraphEdge(
                        id=f"linked:{linked}:{action.subject_id}",
                        upper_id=linked,
                        lower_id=action.subject_id,
                        kind="LINKED_RECORD",
                        label="linked record",
                        title="A record linked to this one has owners the screened graph"
                        " does not show (POSSIBLE_DUPLICATE)",
                    )
                )

    edge_gaps = {edge.id: tuple(sorted(gaps.get(edge.id, []))) for edge in memo.edges}
    if scope == "paths":
        reached = {m.entity_id for m in memo.matches if m.effect != "NONE"}
        chosen = {memo.target_id} | path_nodes | (set(gaps) & set(entities)) | contested | reached
        for edge in memo.edges:
            if edge_gaps[edge.id]:
                chosen |= {edge.upper_id, edge.lower_id}
        chosen = _with_chains(chosen, memo)
        chosen |= {e.upper_id for e in extra if e.lower_id in chosen}
        if chosen == {memo.target_id}:
            # Nothing on a path and no gap (a CLEAR): what was checked is the evidence.
            scope = "checked"
    if scope == "checked":
        chosen = set(entities)

    # The nodes the decision rests on first, then the nearest; the cap cuts the rest.
    must = {memo.target_id} | path_nodes | set(memo.blocked_set) | set(memo.possibly_blocked_set)
    ranked = sorted(
        chosen & set(entities),
        key=lambda nid: (
            nid not in must,
            entities[nid].depth is None,
            entities[nid].depth or 0,
            nid,
        ),
    )
    drawn = set(ranked[:cap])
    not_drawn = len(ranked) - len(drawn)

    level_of: dict[str, int] = {}
    for nid in drawn:
        if entities[nid].depth is not None:
            level_of[nid] = entities[nid].depth
    for edge in extra:
        # A record outside the graph sits beside the record it is linked to.
        if edge.upper_id in drawn and edge.upper_id not in level_of:
            level_of[edge.upper_id] = level_of.get(edge.lower_id, 0)
    nodes = []
    for nid in sorted(drawn, key=lambda n: (level_of.get(n, 0), n)):
        entity = entities[nid]
        state = states[nid]
        badges = [
            word
            for word, shown in (
                (STATE_STYLE[state][2], True),
                ("CONTESTED MATCH", nid in contested),
                ("⋯ DEPTH LIMIT", nid in truncated),
            )
            if word and shown
        ]
        label = _short(entity.legal_name)
        if nid == memo.target_id:
            label = f"TARGET\n{label}"
        if badges:
            label += "\n[" + ", ".join(badges) + "]"
        details = [
            entity.legal_name,
            f"id: {entity.id}",
            f"{entity.entity_type}, {entity.jurisdiction or 'no jurisdiction'}, {entity.status}",
        ]
        if entity.lei:
            details.append(f"LEI {entity.lei} ({entity.lei_status or 'status not stated'})")
        details.append(
            "outside the screened graph"
            if entity.depth is None
            else f"{entity.depth} hop(s) above the target"
        )
        for match in memo.matches:
            if match.entity_id == nid:
                lists = ", ".join(match.list_sources) or "no list"
                details.append(
                    f"designation ({lists}): {match.match_type} {match.confidence:.2f},"
                    f" {'active' if match.is_active else 'inactive'}, effect {match.effect}"
                )
        details += [f"gap: {code}" for code in sorted(gaps.get(nid, []))]
        nodes.append(
            GraphNode(
                id=nid,
                label=label,
                title="\n".join(details),
                state=state,
                level=level_of.get(nid, 0),
                is_target=nid == memo.target_id,
                contested=nid in contested,
                truncated=nid in truncated,
                gaps=tuple(sorted(gaps.get(nid, []))),
            )
        )

    edges = []
    for edge in memo.edges:
        if edge.upper_id not in drawn or edge.lower_id not in drawn:
            continue
        label = (
            edge.rel_type if edge.rel_type == "CONTROLS" else f"{edge.rel_type} {_pct(edge.pct)}"
        )
        if edge.is_ultimate:
            label += " (ultimate)"
        if edge_gaps[edge.id]:
            label += " ⚠"
        title = [label, f"id: {edge.id}", f"source: {edge.provenance.source}"]
        title += [f"gap: {code}" for code in edge_gaps[edge.id]]
        edges.append(
            GraphEdge(
                id=edge.id,
                upper_id=edge.upper_id,
                lower_id=edge.lower_id,
                kind=edge.rel_type,
                label=label,
                title="\n".join(title),
                on_path=edge.id in path_edges,
                is_ultimate=edge.is_ultimate,
                gaps=edge_gaps[edge.id],
            )
        )
    edges += [e for e in extra if e.upper_id in drawn and e.lower_id in drawn]
    edges.sort(key=lambda e: e.id)

    notes = []
    if not_drawn:
        notes.append(f"{not_drawn} more node(s) not drawn (limit {cap}); see the memo.")
    if memo.coverage.down_truncated:
        notes.append("Subsidiaries below the target were capped during the screen.")
    return GraphSpec(nodes, edges, not_drawn=not_drawn, scope=scope, notes=notes)


# --- writing it out -------------------------------------------------------------------------


def _vis_data(spec: GraphSpec) -> dict[str, list[dict]]:
    nodes = []
    for node in spec.nodes:
        fill, shape, _ = STATE_STYLE[node.state]
        nodes.append(
            {
                "id": node.id,
                "label": node.label,
                "title": node.title,
                "level": node.level,
                "shape": shape,
                "color": {"background": fill, "border": "#000000" if node.is_target else "#333333"},
                "borderWidth": 5 if node.is_target else 1,
                "shapeProperties": {"borderDashes": [6, 4] if node.contested else False},
                "font": {"multi": False, "size": 13},
            }
        )
    edges = []
    for edge in spec.edges:
        if edge.kind in ("MATCH", "LINKED_RECORD"):
            colour, dashes = LINK_COLOUR, [4, 4]
        else:
            colour = ULTIMATE_COLOUR if edge.is_ultimate else EDGE_COLOUR
            dashes = EDGE_DASHES[edge.kind]
        edges.append(
            {
                "id": edge.id,
                "from": edge.upper_id,
                "to": edge.lower_id,
                "label": edge.label,
                "title": edge.title,
                "dashes": dashes,
                "width": 4 if edge.on_path else 1 if edge.is_ultimate else 2,
                "color": {"color": colour},
                "arrows": "to",
                "font": {"size": 11, "align": "middle"},
            }
        )
    return {"nodes": nodes, "edges": edges}


OPTIONS = {
    "layout": {
        "hierarchical": {
            "enabled": True,
            # The target at the bottom, its owners above it, as in an ownership chart.
            "direction": "DU",
            "levelSeparation": 140,
            "nodeSpacing": 200,
            "sortMethod": "directed",
        }
    },
    "physics": {"enabled": False},
    "interaction": {"hover": True, "navigationButtons": True, "keyboard": True},
    "edges": {"smooth": {"type": "cubicBezier", "forceDirection": "vertical"}},
}

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<style>{css}
html, body {{ margin: 0; background: #ffffff; }}
#graph {{ width: 100%; height: {height}px; border: 1px solid #dddddd; }}
div.vis-tooltip {{ white-space: pre-line; max-width: 420px; font-family: sans-serif; }}
</style>
<script>{js}</script>
</head><body>
<div id="graph" role="img" aria-label="Evidence graph; the same edges are listed as a table"></div>
<script>
const data = {data};
const options = {options};
new vis.Network(document.getElementById("graph"),
  {{nodes: new vis.DataSet(data.nodes), edges: new vis.DataSet(data.edges)}}, options);
</script>
</body></html>
"""


def _safe_json(value: object) -> str:
    """JSON that cannot end the script element it is written into, whatever a name holds."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    for char, escaped in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026")):
        text = text.replace(char, escaped)
    return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _vis_asset(name: str) -> str:
    return (resources.files("pyvis") / "templates" / "lib" / "vis-9.1.2" / name).read_text(
        encoding="utf-8"
    )


def to_html(spec: GraphSpec, height: int = 620) -> str:
    """A self-contained page: vis-network inline, the graph as JSON, nothing fetched."""
    return _PAGE.format(
        css=_vis_asset("vis-network.css"),
        js=_vis_asset("vis-network.min.js"),
        height=height,
        data=_safe_json(_vis_data(spec)),
        options=_safe_json(OPTIONS),
    )


def edge_rows(spec: GraphSpec) -> list[dict[str, str]]:
    """The drawing as a table: the same edges, readable without the canvas."""
    names = {node.id: node.label.replace("\n", " ") for node in spec.nodes}
    return [
        {
            "from (owner, parent, controller)": names[edge.upper_id],
            "relation": edge.label,
            "to": names[edge.lower_id],
            "on an evidence path": "yes" if edge.on_path else "",
            "gaps": ", ".join(edge.gaps),
        }
        for edge in spec.edges
    ]


LEGEND = [
    ("◆ red", "designated: an active designation blocks it"),
    ("● red", "blocked by ownership (OFAC 50 % rule)"),
    ("▲ amber", "possibly blocked: presumed majority or a review-band match"),
    ("■ purple", "designated only on a list this rule pack does not apply"),
    ("▭ white", "outside the screened graph: whose designation it is, or a linked record"),
    ("● green", "checked, nothing found"),
    ("thick outline", "the target"),
    ("dashed outline", "a contested match the reviewer settles"),
    ("solid / long dash / dotted line", "OWNS / CONSOLIDATED_BY / CONTROLS"),
    ("thick line", "on an evidence path; thin grey: an ultimate-parent record"),
    ("blue dashed line", "a designation that crossed a link, or a linked record"),
]
