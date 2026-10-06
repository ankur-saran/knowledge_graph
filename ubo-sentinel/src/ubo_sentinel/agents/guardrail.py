"""Guardrail Agent: the memo is checked before it becomes part of a decision.

Two guardrails, kept apart on purpose.

The decision guardrail reads only what does not depend on the role: the memo,
the rule result and the store. A failure is written into the decision and
raises its recommendation to at least REVIEW; it never lowers one.

The render guardrail reads what one role is about to be shown. A failure means
the output is not shown. It cannot change a decision: the same screen must
give an analyst and a reviewer the same decision.

A warning names the kind of fault, never the value: a warning is stored
unmasked in the decision and shown to every role.
"""

import re
from decimal import Decimal, InvalidOperation
from typing import Any

import jinja2

from ubo_sentinel.agents.base import BaseAgent, PipelineState, ScreenContext
from ubo_sentinel.agents.explainer import cite, format_score, render_memo
from ubo_sentinel.graph import queries
from ubo_sentinel.graph.store import GraphStore
from ubo_sentinel.graph.views import masked_values
from ubo_sentinel.models.decision import Decision
from ubo_sentinel.models.memo import Memo
from ubo_sentinel.models.ontology import OntologyConfig
from ubo_sentinel.models.rule_result import RuleResult

_CODE_SPAN = re.compile(r"`([^`]*)`")
_BOLD_SPAN = re.compile(r"\*\*(.+?)\*\*")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _leaves(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _leaves(item)]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _leaves(item)]
    return [value]


def check_prose(text: str, memo: Memo) -> list[str]:
    """Faults of a text about a memo: a name, id, date or number the memo does not hold.

    The text follows the template's conventions: a name is bold, an id or a
    code is in backticks. It is checked the same way whoever wrote it, so the
    check also serves prose a model writes (Step 13).
    """
    leaves = _leaves(memo.model_dump(mode="json"))
    strings = {leaf for leaf in leaves if isinstance(leaf, str)}
    # What the renderer makes of a value: a short hash, a citation's record.
    strings.add(memo.rule_pack.hash[:16])
    for citation in memo.citations():
        strings.update(_CODE_SPAN.findall(cite(citation)))
    numbers: set[Decimal] = set()
    for leaf in leaves:
        if isinstance(leaf, bool) or leaf is None:
            continue
        if isinstance(leaf, float):
            numbers.add(Decimal(format_score(leaf)))
        try:
            numbers.add(Decimal(str(leaf)))
        except InvalidOperation:
            pass

    faults = set()
    if any(span not in strings for span in _CODE_SPAN.findall(text)):
        faults.add("the memo text has an id or code that the memo does not hold")
    rest = _CODE_SPAN.sub(" ", text)
    if any(span not in strings for span in _BOLD_SPAN.findall(rest)):
        faults.add("the memo text has a name that the memo does not hold")
    rest = _BOLD_SPAN.sub(" ", rest)
    if any(found not in strings for found in _DATE.findall(rest)):
        faults.add("the memo text has a date that the memo does not hold")
    rest = _DATE.sub(" ", rest)
    if any(Decimal(found) not in numbers for found in _NUMBER.findall(rest)):
        faults.add("the memo text has a number that the memo does not hold")
    return sorted(faults)


def _agrees(memo: Memo, state: PipelineState) -> list[str]:
    """Fields in which the memo differs from what the screen concluded."""
    result: RuleResult | None = state.rule_result
    if result is None:
        reason = "ENTITY_NOT_FOUND" if state.target_id is None else "EVIDENCE_LIMIT_EXCEEDED"
        expected: dict[str, Any] = {
            "target_id": state.target_id,
            "recommendation": "REVIEW",
            "reasons": [reason],
            "blocked_set": [],
            "possibly_blocked_set": [],
            "paths": [],
        }
    else:
        expected = {
            "target_id": result.target_id,
            "recommendation": result.recommendation,
            "reasons": result.reasons,
            "blocked_set": result.blocked_set,
            "possibly_blocked_set": result.possibly_blocked_set,
            "paths": result.paths,
            "aggregate_pct": result.aggregate_pct,
            "effective_exposure": result.effective_exposure,
        }
        if state.target_id != result.target_id:
            return ["target_id"]
    return [name for name, value in expected.items() if getattr(memo, name) != value]


def check_memo(memo: Memo, state: PipelineState, store: GraphStore) -> list[str]:
    """The decision guardrail: every fault of the memo, as warnings. Empty when it holds."""
    warnings: set[str] = set()

    if Memo.model_validate(memo.model_dump(mode="json")) != memo:
        warnings.add("the memo does not survive a round trip through JSON")
    warnings.update(f"uncited claim: {item.split()[0]}" for item in memo.uncited())
    if memo.dangling():
        warnings.add("the memo refers to an entity or edge it does not describe")
    warnings.update(f"the memo disagrees with the rules on {name}" for name in _agrees(memo, state))

    # Every citation leads to a record of the screened set.
    snapshots = set(store.snapshot_set.snapshot_ids.values())
    checked = set()
    for citation in memo.citations():
        if citation.provenance is None:
            if citation.ref_id not in snapshots:
                warnings.add("a citation names a snapshot outside the screened set")
            continue
        key = citation.provenance.sort_key()
        if key in checked:
            continue
        checked.add(key)
        if queries.get_source_record(store, citation.provenance) is None:
            warnings.add("a citation names a record that does not exist")

    try:
        warnings.update(check_prose(render_memo(memo), memo))
    except (jinja2.TemplateError, LookupError):
        # A claim without what its sentence needs: the memo cannot be shown as it is.
        warnings.add("the memo cannot be rendered")
    return sorted(warnings)


def check_render(text: str, decision: Decision, role: str, ontology: OntologyConfig) -> list[str]:
    """The render guardrail: what the text shows that the role must not see.

    Returns the kinds of leak, not the values.
    """
    leaked = [value for value in masked_values(decision, role, ontology) if value in text]
    return ["the output shows a value that is masked for this role"] if leaked else []


class GuardrailAgent(BaseAgent):
    name = "Guardrail"
    requires = ("memo",)
    provides = ("guardrail_warnings",)
    runs_after_halt = True

    def run(self, state: PipelineState, ctx: ScreenContext) -> PipelineState:
        return state.with_(guardrail_warnings=check_memo(state.memo, state, ctx.store))
