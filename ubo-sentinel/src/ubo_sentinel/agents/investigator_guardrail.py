"""What an investigation may say (Step 14.5): every finding checked against its citations.

A finding is free of names and figures by construction: its text holds slots
(`{e:<id>}`, `{v:<call>.<pointer>}`), by the rules of `llm_prose.check_slotted`,
and code fills them. This module checks that each slot resolves in a result the
finding cites, that each subject is shown by one, that the tools cited can show
a claim of that code, and that the words between the slots state no fact and no
outcome. A value slot reaches a field of a listed kind and no free text: a name
written to steer a model must not reach a reader through a slot.

The first pass is held to the same checks: a guardrail that has never failed is
not known to work (7.7). A finding that fails is dropped and its kind of fault
kept, never a value. The check reads stored data only, so a replay runs it again.
"""

import re
from collections.abc import Iterator
from typing import Any

from ubo_sentinel.agents.investigator_tools import TOOLS, result_ids
from ubo_sentinel.agents.llm_prose import MIN_NAME, OUTCOME_WORDS, SLOT, check_bare, memo_names
from ubo_sentinel.graph.views import MASK
from ubo_sentinel.llm.egress import egress_memo
from ubo_sentinel.models.decision import Decision
from ubo_sentinel.models.investigation import Finding, SuggestedAction, ToolCallRecord
from ubo_sentinel.models.ontology import OntologyConfig
from ubo_sentinel.pipeline.name_match import normalise_name

# The words of a finding, between its slots.
MAX_TEXT = 300
# A value a slot may fill in: a code or a figure, not a sentence.
MAX_VALUE = 60
# The recommendations, written as the memo writes them, and words that argue about
# one. `OUTCOME_WORDS` holds the rest.
_RECOMMENDATION = re.compile(r"\b(CLEAR|REVIEW|ESCALATE)\b")
_JUDGEMENT = re.compile(
    r"\b(false[- ]positive|not\s+(blocked|sanctioned|designated)"
    r"|no\s+(sanctions\s+)?(exposure|concern)|should\s+(be|not)|must\s+(be|not))\b",
    re.IGNORECASE,
)
# The tools that can show a claim: one of each group must be cited.
_OWNERS = ("get_common_controllers", "get_sibling_entities", "get_direct_owners")
FINDING_TOOLS: dict[str, tuple[tuple[str, ...], ...]] = {
    "SHARED_ADDRESS": (("get_entities_sharing_address",),),
    "COMMON_CONTROLLER": (_OWNERS,),
    "DESIGNATED_SIBLING": (("get_sibling_entities",), ("get_designations_of",)),
    "PATH_BETWEEN": (("find_paths_between",),),
    "HISTORY_CHANGE": (("get_entity_history",),),
    "LINKED_RECORD_OWNERS": (("get_direct_owners",),),
    "NO_FURTHER_LINK_FOUND": (),
}
# The fields of a result a value slot may read: codes, flags, figures, dates. Names,
# aliases, addresses, reasons and published records are text a source wrote.
VALUE_FIELDS = frozenset(
    {
        "as_of",
        "basis",
        "category",
        "cause",
        "changed_fields",
        "confidence",
        "country",
        "domain",
        "entity_type",
        "fund_manager",
        "is_active",
        "is_ultimate",
        "jurisdiction",
        "jurisdictions_agree",
        "kind",
        "kinds",
        "lei_status",
        "list_date",
        "list_source",
        "mass",
        "match_type",
        "not_listed",
        "pct",
        "program",
        "rel_type",
        "source",
        "status",
        "total",
        "truncated",
    }
)
_MISSING = object()


def resolve_value(pointer: str, result: dict[str, Any]) -> Any:
    """A JSON pointer's value in a call's result, or `_MISSING`."""
    if not pointer.startswith("/"):
        return _MISSING
    found: Any = result
    for raw in pointer[1:].split("/"):
        step = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(found, dict) and step in found:
            found = found[step]
        elif isinstance(found, list) and step.isdigit() and int(step) < len(found):
            found = found[int(step)]
        else:
            return _MISSING
    return found


def _field(pointer: str) -> str:
    """The field a pointer ends on, past any list index."""
    steps = [step for step in pointer.split("/") if step and not step.isdigit()]
    return steps[-1] if steps else ""


def _value_faults(value: str, cited: dict[str, ToolCallRecord]) -> set[str]:
    call_id, _, pointer = value.partition(".")
    record = cited.get(call_id)
    if record is None:
        return {"a value slot of a call the finding does not cite"}
    found = resolve_value(pointer, record.result())
    if found is _MISSING or found is None or isinstance(found, dict | list):
        return {"a value slot that resolves to no value"}
    if _field(pointer) not in VALUE_FIELDS:
        return {"a value slot on a field of free text"}
    if len(str(found)) > MAX_VALUE:
        return {"a value too long to be one"}
    if OUTCOME_WORDS.search(str(found)):
        return {"a value that states an outcome"}
    return set()


def _ids_of(record: ToolCallRecord) -> set[str]:
    """What a call shows: the ids it asked about, and those its result holds."""
    ids = result_ids(record.items)
    tool = TOOLS.get(record.tool)
    for name in tool.id_arguments if tool else ():
        value = record.arguments.get(name)
        ids.update([value] if isinstance(value, str) else value or [])
    return ids


def _names_in(value: Any) -> Iterator[str]:
    if isinstance(value, list):
        for item in value:
            yield from _names_in(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key == "legal_name" and isinstance(item, str):
                yield item
            elif key == "aliases" and isinstance(item, list):
                yield from item
            else:
                yield from _names_in(item)


def _names(calls: list[ToolCallRecord], decision: Decision) -> set[str]:
    """Every name the decision and the calls hold: none may be written outside a slot."""
    found = memo_names(decision)
    for record in calls:
        folded = (normalise_name(name) for name in _names_in(record.items) if name != MASK)
        found.update(name for name in folded if len(name) >= MIN_NAME)
    return found


def _finding_faults(
    finding: Finding, calls: dict[str, ToolCallRecord], names: set[str]
) -> set[str]:
    faults: set[str] = set()
    if not finding.subjects:
        faults.add("a finding about no entity")
    if not finding.cites:
        faults.add("a finding that cites nothing")
    if any(call_id not in calls for call_id in finding.cites):
        faults.add("a citation of no call")
    cited = {i: calls[i] for i in finding.cites if i in calls}
    if any(record.fault for record in cited.values()):
        faults.add("a citation of a refused call")
    answered = {i: record for i, record in cited.items() if record.fault is None}
    tools = {record.tool for record in answered.values()}
    if not all(tools.intersection(group) for group in FINDING_TOOLS[finding.code]):
        faults.add("a finding that cites no tool that can show it")
    shown = set().union(*map(_ids_of, answered.values())) if answered else set()
    if not set(finding.subjects) <= shown:
        faults.add("a subject no cited call shows")
    for kind, value in SLOT.findall(finding.text):
        if kind == "e":
            if value not in shown:
                faults.add("an entity slot no cited call shows")
        elif kind == "v":
            faults |= _value_faults(value, answered)
        else:
            faults.add("a slot of an unknown kind")
    bare = SLOT.sub(" ", finding.text)
    faults |= check_bare(bare, names)
    if len(bare) > MAX_TEXT:
        faults.add("a finding that is too long")
    if _RECOMMENDATION.search(bare) or _JUDGEMENT.search(bare):
        faults.add("a word that states an outcome")
    return faults


def check_findings(
    findings: list[Finding], calls: list[ToolCallRecord], decision: Decision
) -> tuple[list[Finding], list[str]]:
    """(the findings that pass, the kinds of fault of those that do not)."""
    by_id = {record.call_id: record for record in calls}
    names = _names(calls, decision)
    kept = []
    faults: set[str] = set()
    for finding in findings:
        found = _finding_faults(finding, by_id, names)
        faults |= found
        if not found:
            kept.append(finding)
    return kept, sorted(faults)


def check_actions(
    actions: list[SuggestedAction],
    calls: list[ToolCallRecord],
    decision: Decision,
    ontology: OntologyConfig,
) -> tuple[list[SuggestedAction], list[str]]:
    """(the actions that pass, the kinds of fault). An action names only what it was given."""
    memo = egress_memo(decision, ontology)
    given = {entity.id for entity in memo.entities}
    for record in calls:
        if record.fault is None:
            given |= _ids_of(record)
    kept = []
    faults: set[str] = set()
    for action in actions:
        found: set[str] = set()
        if action.code == "SCREEN_RELATED_ENTITY" and action.entity_id is None:
            found.add("a suggested screen of no entity")
        if action.entity_id is not None and action.entity_id not in given:
            found.add("an action naming an entity it was not given")
        if (action.code == "REQUEST_DOCUMENT") != (action.document is not None):
            found.add("a document on the wrong action")
        faults |= found
        if not found:
            kept.append(action)
    return kept, sorted(faults)
