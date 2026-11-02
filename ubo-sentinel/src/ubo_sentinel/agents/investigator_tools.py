"""What the investigator's model may call (Step 14.3): an allow-list over `QUERIES`.

`TOOLS` names ten of the fifteen queries. The others take an object, expose the
log, return a whole ego graph, or belong to the monitor. No tool writes: none
screens, reviews or appends. Strict schemas cannot state a limit, so every
limit here is enforced in code.

A `ToolBox` is one investigation's use of them:

- Every call reads through one `GraphView("analyst")` on the decision's set,
  whoever asked (13.0). The model names a masked person by the pseudonym the
  memo or a result gave it.
- A result is projected to what the model needs, cleaned (`llm/egress.py`) and
  capped, and what a cap left out is counted. It is then scanned for the masked
  values of the decision and of every masked node this view has met; a result
  that holds one is refused, by kind and never by value.
- A call may name only ids it has been given, in the memo or in a result: read
  from their id fields, never from a name, so a name written to look like an id
  reaches nothing. A record is named by the ref a result gave it.
- The same call again returns the earlier result. It still counts as a step:
  the budget is the caller's.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ubo_sentinel.graph.views import MASK, GraphView, masked_values
from ubo_sentinel.llm.egress import EGRESS_ROLE, EgressRefused, clean, egress_id, egress_memo
from ubo_sentinel.llm.tools import ToolSpec
from ubo_sentinel.models.canonical import canonical_json, short_id
from ubo_sentinel.models.change import Change, EntityHistoryStep
from ubo_sentinel.models.decision import Decision
from ubo_sentinel.models.entity import Entity
from ubo_sentinel.models.graph import SanctionMatch, SharedAddress, SubGraph
from ubo_sentinel.models.investigation import (
    DOCUMENT_KINDS,
    FINDING_CODES,
    SUGGESTED_CODES,
    ToolCallRecord,
    ToolFault,
)
from ubo_sentinel.models.memo import Memo
from ubo_sentinel.models.ontology import OntologyConfig
from ubo_sentinel.models.provenance import Provenance
from ubo_sentinel.models.relationship import Relationship
from ubo_sentinel.pipeline.context import ContextNotBuilt

SUBMIT_FINDINGS = "submit_findings"
# Items a result lists, and what a query is asked for: one more shows that more exist.
RESULT_LIMIT = 20
# A result's size as sent. Items past it are counted, not sent.
MAX_RESULT_BYTES = 12_000
# Ids one call may name.
MAX_IDS = 10
MAX_ALIASES = 5
MAX_HISTORY_SETS = 5

# The fields of a projected result that hold entity ids. `before` and `after` are
# a change's raw rows and `fields` a published record: neither is read for ids.
_ID_FIELDS = frozenset(
    {"id", "entity_id", "designated_entity_id", "subject_id", "object_id"}
)  # fmt: skip
_ID_LIST_FIELDS = frozenset({"node_ids", "held_ids", "anchor_ids"})
_RAW_FIELDS = frozenset({"before", "after", "fields"})

STRING = {"type": "string"}
STRINGS = {"type": "array", "items": STRING}
INTEGER = {"type": "integer"}


class _Refused(Exception):
    def __init__(self, fault: ToolFault) -> None:
        super().__init__(fault)
        self.fault: ToolFault = fault


def _spec(name: str, description: str, **properties: dict[str, Any]) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": sorted(properties),
            "additionalProperties": False,
        },
    )


@dataclass(frozen=True)
class Tool:
    spec: ToolSpec
    # The query it reads: a name in `graph.queries.QUERIES`.
    query: str
    # Its arguments that name entities.
    id_arguments: tuple[str, ...]
    run: Callable[..., list[dict[str, Any]]]


class ToolBox:
    def __init__(self, view: GraphView, decision: Decision, ontology: OntologyConfig) -> None:
        if view.role != EGRESS_ROLE:
            raise ValueError(f"A model reads the {EGRESS_ROLE}'s view, not the {view.role}'s.")
        if view.store.set_id != decision.snapshot_set_id:
            raise ValueError("The view is not on the set the decision read.")
        self.view = view
        self.decision = decision
        self.ontology = ontology
        self.calls: list[ToolCallRecord] = []
        # Every id the model has been given, as it knows them.
        self.seen_ids: set[str] = set()
        # Ref -> the record it names, for each citation the model has been given.
        self.records: dict[str, Provenance] = {}
        self._cited: dict[str, Provenance] = {}
        self._done: dict[bytes, ToolCallRecord] = {}
        self._seed()

    def _seed(self) -> None:
        for entity in Memo.model_validate(self.decision.memo_json).entities:
            alias = egress_id(entity.id, entity.entity_type, self.ontology)
            if alias != entity.id:
                self.view.pseudonym(entity.id, entity.entity_type)
        memo = egress_memo(self.decision, self.ontology)
        self.seen_ids.update(entity.id for entity in memo.entities)
        self.seen_ids.update(memo.blocked_set + memo.possibly_blocked_set)
        self.seen_ids.update(i for m in memo.matches for i in (m.entity_id, m.designated_entity_id))
        if memo.target_id:
            self.seen_ids.add(memo.target_id)

    def memo_records(self) -> list[dict[str, Any]]:
        """The refs of the records the memo cites, for the model's first turn."""
        found: dict[str, Provenance] = {}

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                if value.keys() == Provenance.model_fields.keys():
                    record = Provenance.model_validate(value)
                    found[record.source_record_id, record.source, record.snapshot_id] = record
                    return
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(egress_memo(self.decision, self.ontology).model_dump(mode="json"))
        rows = []
        for _, record in sorted(found.items()):
            if record.source_record_id == MASK:
                continue
            ref = self._ref(record)
            self.records[ref] = record
            rows.append(
                {"ref": ref, "source": record.source, "source_record_id": record.source_record_id}
            )
        return rows

    def run(self, name: str, arguments: dict[str, Any]) -> ToolCallRecord:
        """One call. Never raises for what a model may ask: a refusal is a record."""
        call_id = f"c{len(self.calls) + 1}"
        key = canonical_json([name, arguments])
        earlier = self._done.get(key)
        if earlier is not None:
            record = earlier.model_copy(update={"call_id": call_id, "repeat_of": earlier.call_id})
        else:
            record = self._execute(call_id, name, arguments)
            if record.fault is None:
                self._done[key] = record
        self.calls.append(record)
        return record

    def refuse(self, name: str, arguments: dict[str, Any], fault: ToolFault) -> ToolCallRecord:
        """A call the caller will not run. It is on the record all the same."""
        call_id = f"c{len(self.calls) + 1}"
        record = ToolCallRecord(call_id=call_id, tool=name, arguments=arguments, fault=fault)
        self.calls.append(record)
        return record

    def holds_masked(self, data: Any) -> bool:
        """Whether a document holds a value the analyst may not see."""
        try:
            self._scan(data)
        except EgressRefused:
            return True
        return False

    def _execute(self, call_id: str, name: str, arguments: dict[str, Any]) -> ToolCallRecord:
        refused = dict(call_id=call_id, tool=name, arguments=arguments)
        tool = TOOLS.get(name)
        if tool is None:
            return ToolCallRecord(**refused, fault="UNKNOWN_TOOL")
        try:
            self._check(tool, arguments)
            items, truncated = self._fit(clean(tool.run(self, **arguments)))
            self._scan(items)
        except _Refused as exc:
            return ToolCallRecord(**refused, fault=exc.fault)
        except ContextNotBuilt:
            return ToolCallRecord(**refused, fault="NO_CONTEXT")
        except EgressRefused:
            return ToolCallRecord(**refused, fault="EGRESS_REFUSED")
        except ValueError:  # a query's own bound: a depth, say
            return ToolCallRecord(**refused, fault="BAD_ARGUMENT")
        self._see(items)
        return ToolCallRecord(**refused, items=items, truncated=truncated)

    def _check(self, tool: Tool, arguments: dict[str, Any]) -> None:
        """The schema again (a recording or a fake is not held to it), then the ids."""
        properties = tool.spec.input_schema["properties"]
        if arguments.keys() != properties.keys():
            raise _Refused("BAD_ARGUMENT")
        for name, value in arguments.items():
            kind = properties[name]["type"]
            valid = (
                type(value) is int
                if kind == "integer"
                else isinstance(value, str)
                if kind == "string"
                else isinstance(value, list)
                and len(value) <= MAX_IDS
                and all(isinstance(item, str) for item in value)
            )
            if not valid:
                raise _Refused("BAD_ARGUMENT")
        for name in tool.id_arguments:
            value = arguments[name]
            if not set([value] if isinstance(value, str) else value) <= self.seen_ids:
                raise _Refused("UNSEEN_ID")

    def _fit(self, found: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        """The items that fit, and how many were left out, here or by a query's own limit."""
        items: list[dict[str, Any]] = []
        size = 0
        for item in found[:RESULT_LIMIT]:
            size += len(canonical_json(item))
            if size > MAX_RESULT_BYTES:
                break
            items.append(item)
        return items, len(found) - len(items) + sum(i.get("not_listed", 0) for i in items)

    def _scan(self, items: Any) -> None:
        """Refuse a result that holds a value the analyst may not see. Whole words: a short
        alias is also the inside of many a company's name. An address is not among the
        values: the view masks a person's, and a company at that place states its own."""
        values = {*masked_values(self.decision, EGRESS_ROLE, self.ontology)}
        values.update(self.view.masked_values_met())
        values.discard(MASK)
        pattern = "|".join(re.escape(value) for value in sorted(values))
        text = canonical_json(items).decode("utf-8")
        if pattern and re.search(rf"(?<!\w)(?:{pattern})(?!\w)", text):
            raise EgressRefused("the result holds a value the analyst may not see")

    def _see(self, value: Any) -> None:
        """Note the ids and the refs of what is being sent, and nothing that was cut."""
        if isinstance(value, list):
            for item in value:
                self._see(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key in _RAW_FIELDS:
                    continue
                if key in _ID_FIELDS and isinstance(item, str):
                    self.seen_ids.add(item)
                elif key in _ID_LIST_FIELDS:
                    self.seen_ids.update(item)
                elif key == "ref":
                    self.records[item] = self._cited[item]
                else:
                    self._see(item)

    # --- projections ---------------------------------------------------------------------

    def _ref(self, record: Provenance) -> str:
        return f"r{short_id([record.source, record.source_record_id, record.snapshot_id])[:10]}"

    def _cite(self, record: Provenance, *ends: str) -> dict[str, Any]:
        """A citation the model can ask for. An edge's record names its ends, so one with
        a masked end is masked with it."""
        if record.source_record_id == MASK or any(self.view.is_pseudonym(end) for end in ends):
            return {"source": record.source, "as_of": record.as_of.isoformat(), "masked": True}
        ref = self._ref(record)
        self._cited[ref] = record
        return {"ref": ref, "source": record.source, "as_of": record.as_of.isoformat()}

    def _entity(self, entity: Entity) -> dict[str, Any]:
        return {
            "id": entity.id,
            "legal_name": entity.legal_name,
            "aliases": entity.aliases[:MAX_ALIASES],
            "jurisdiction": entity.jurisdiction,
            "entity_type": entity.entity_type,
            "status": entity.status,
            "lei": entity.lei,
            "lei_status": entity.lei_status,
            "repex_reason": entity.repex_reason,
            "record": self._cite(entity.provenance),
        }

    def _edge(self, edge: Relationship) -> dict[str, Any]:
        return {
            "edge_id": edge.id,
            "subject_id": edge.subject_id,
            "object_id": edge.object_id,
            "rel_type": edge.rel_type,
            "pct": str(edge.pct) if edge.pct is not None else None,
            "basis": edge.basis,
            "is_ultimate": edge.is_ultimate,
            "record": self._cite(edge.provenance, edge.subject_id, edge.object_id),
        }

    def _designation(self, match: SanctionMatch) -> dict[str, Any]:
        return {
            "entity_id": match.entity_id,
            "designated_entity_id": match.designated_entity_id,
            "match_type": match.match_type,
            "confidence": match.confidence,
            "is_active": match.is_active,
            "jurisdictions_agree": match.jurisdictions_agree,
            "designations": [
                {
                    "program": sanction.program,
                    "list_source": sanction.list_source,
                    "list_date": sanction.list_date.isoformat() if sanction.list_date else None,
                    "is_active": sanction.is_active,
                    "record": self._cite(sanction.provenance),
                }
                for sanction in match.sanction_records
            ],
        }

    def _place(self, place: SharedAddress) -> dict[str, Any]:
        return {
            "address": place.address,
            "kinds": place.kinds,
            "country": place.country,
            "total": place.total,
            "mass": place.mass,
            "not_listed": place.total - len(place.peers),
            "record": self._cite(place.provenance),
            "peers": [
                {
                    "entity": self._entity(peer.entity),
                    "kinds": peer.kinds,
                    "address": peer.address,
                    "record": self._cite(peer.provenance),
                }
                for peer in place.peers
            ],
        }

    def _change(self, change: Change) -> dict[str, Any]:
        return {
            "domain": change.domain,
            "kind": change.kind,
            "cause": change.cause,
            "anchor_ids": change.anchor_ids,
            "changed_fields": change.fields,
            "before": change.before,
            "after": change.after,
        }

    def _step(self, step: EntityHistoryStep) -> dict[str, Any]:
        return {
            "prev_set_id": step.prev_set_id,
            "curr_set_id": step.curr_set_id,
            "refused": step.refused,
            "not_listed": max(0, len(step.changes) - RESULT_LIMIT),
            "changes": [self._change(change) for change in step.changes[:RESULT_LIMIT]],
        }


# --- the tools ------------------------------------------------------------------------------


def _get_entity(box: ToolBox, entity_id: str) -> list[dict[str, Any]]:
    entity = box.view.get_entity_by_id(entity_id)
    return [box._entity(entity)] if entity else []


def _get_direct_owners(box: ToolBox, entity_id: str) -> list[dict[str, Any]]:
    return [
        {"owner": box._entity(item.owner), "edge": box._edge(item.relationship)}
        for item in box.view.get_direct_owners(entity_id)
    ]


def _get_reporting_exceptions(box: ToolBox, entity_id: str) -> list[dict[str, Any]]:
    return [
        {
            "entity_id": record.entity_id,
            "category": record.category,
            "reason": record.reason,
            "record": box._cite(record.provenance, record.entity_id),
        }
        for record in box.view.get_reporting_exceptions(entity_id)
    ]


def _get_designations_of(box: ToolBox, entity_ids: list[str]) -> list[dict[str, Any]]:
    """The query reads the ids of a subgraph: these entities, and no walk."""
    found = [box.view.get_entity_by_id(i) for i in sorted(set(entity_ids))]
    nodes = [entity for entity in found if entity]
    if not nodes:
        return []
    subgraph = SubGraph(
        target_id=nodes[0].id,
        snapshot_set_id=box.view.store.set_id,
        max_depth_up=0,
        max_depth_down=0,
        nodes=nodes,
        depth_up={node.id: 0 for node in nodes},
    )
    return [box._designation(match) for match in box.view.get_sanctioned_nodes(subgraph)]


def _get_source_record(box: ToolBox, record: str) -> list[dict[str, Any]]:
    if record not in box.records:
        raise _Refused("UNSEEN_RECORD")
    published = box.view.get_source_record(box.records[record])
    return [{"record": record, "fields": published}] if published else []


def _get_entities_sharing_address(box: ToolBox, entity_id: str) -> list[dict[str, Any]]:
    return [
        box._place(place)
        for place in box.view.get_entities_sharing_address(entity_id, RESULT_LIMIT)
    ]


def _get_common_controllers(box: ToolBox, entity_ids: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "controller": box._entity(item.controller),
            "held_ids": item.held_ids,
            "edges": [box._edge(edge) for edge in item.edges],
            "fund_manager": item.fund_manager,
        }
        for item in box.view.get_common_controllers(entity_ids, RESULT_LIMIT + 1)
    ]


def _get_sibling_entities(box: ToolBox, entity_id: str) -> list[dict[str, Any]]:
    return [
        {
            "parent": box._entity(group.parent),
            "edge": box._edge(group.relationship),
            "total": group.total,
            "not_listed": group.total - len(group.siblings),
            "fund_manager": group.fund_manager,
            "siblings": [
                {"entity": box._entity(held.entity), "edge": box._edge(held.relationship)}
                for held in group.siblings
            ],
        }
        for group in box.view.get_sibling_entities(entity_id, RESULT_LIMIT)
    ]


def _find_paths_between(
    box: ToolBox, source_id: str, target_id: str, max_depth: int
) -> list[dict[str, Any]]:
    return [
        {"node_ids": path.node_ids, "edge_ids": path.edge_ids}
        for path in box.view.find_paths_between(source_id, target_id, max_depth, RESULT_LIMIT + 1)
    ]


def _get_entity_history(box: ToolBox, entity_id: str) -> list[dict[str, Any]]:
    return [box._step(step) for step in box.view.get_entity_history(entity_id, MAX_HISTORY_SETS)]


_ENTITY = {"entity_id": STRING}
TOOLS: dict[str, Tool] = {
    tool.spec.name: tool
    for tool in (
        Tool(
            _spec("get_entity", "One entity by id.", **_ENTITY),
            "get_entity_by_id",
            ("entity_id",),
            _get_entity,
        ),
        Tool(
            _spec(
                "get_direct_owners",
                "The direct owners, parents and controllers of an entity, with their edges.",
                **_ENTITY,
            ),
            "get_direct_owners",
            ("entity_id",),
            _get_direct_owners,
        ),
        Tool(
            _spec(
                "get_reporting_exceptions",
                "Why an entity reports no parent, as its record states.",
                **_ENTITY,
            ),
            "get_reporting_exceptions",
            ("entity_id",),
            _get_reporting_exceptions,
        ),
        Tool(
            _spec(
                "get_designations_of",
                f"The sanctions designations that reach these entities, at most {MAX_IDS} ids.",
                entity_ids=STRINGS,
            ),
            "get_sanctioned_nodes",
            ("entity_ids",),
            _get_designations_of,
        ),
        Tool(
            _spec(
                "get_source_record",
                "The published record behind a citation, by the ref a result gave it."
                " A record that concerns a masked party comes back without its fields.",
                record=STRING,
            ),
            "get_source_record",
            (),
            _get_source_record,
        ),
        Tool(
            _spec(
                "get_entities_sharing_address",
                "Each place an entity's records state, and the other entities at it. A place"
                " marked mass is a registered office or a formation agent's: it is counted,"
                " not listed, and sharing it says little.",
                **_ENTITY,
            ),
            "get_entities_sharing_address",
            ("entity_id",),
            _get_entities_sharing_address,
        ),
        Tool(
            _spec(
                "get_common_controllers",
                "Direct owners, parents or controllers shared by two or more of these"
                f" entities, at most {MAX_IDS} ids. fund_manager marks a manager of funds:"
                " sharing one says little.",
                entity_ids=STRINGS,
            ),
            "get_common_controllers",
            ("entity_ids",),
            _get_common_controllers,
        ),
        Tool(
            _spec(
                "get_sibling_entities",
                "Each direct owner, parent or controller of an entity, and the others it holds.",
                **_ENTITY,
            ),
            "get_sibling_entities",
            ("entity_id",),
            _get_sibling_entities,
        ),
        Tool(
            _spec(
                "find_paths_between",
                "Chains of direct edges between two entities, either way down, shortest first."
                " max_depth is 1 to 10 hops.",
                source_id=STRING,
                target_id=STRING,
                max_depth=INTEGER,
            ),
            "find_paths_between",
            ("source_id", "target_id"),
            _find_paths_between,
        ),
        Tool(
            _spec(
                "get_entity_history",
                "What changed about an entity from each earlier built snapshot set to the next.",
                **_ENTITY,
            ),
            "get_entity_history",
            ("entity_id",),
            _get_entity_history,
        ),
    )
}

SUBMIT_SPEC = _spec(
    SUBMIT_FINDINGS,
    "Finish the investigation: your findings and suggested actions. Call it once, last.",
    findings={
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "enum": list(FINDING_CODES)},
                "subjects": STRINGS,
                "text": STRING,
                "cites": STRINGS,
            },
            "required": ["cites", "code", "subjects", "text"],
            "additionalProperties": False,
        },
    },
    suggested_actions={
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "enum": list(SUGGESTED_CODES)},
                "entity_id": {"type": ["string", "null"]},
                "document": {"type": ["string", "null"], "enum": [*DOCUMENT_KINDS, None]},
            },
            "required": ["code", "document", "entity_id"],
            "additionalProperties": False,
        },
    },
)


def tool_specs() -> tuple[ToolSpec, ...]:
    """What the model is offered, sorted by name: the same tools are the same bytes."""
    return tuple(sorted((*(t.spec for t in TOOLS.values()), SUBMIT_SPEC), key=lambda s: s.name))
