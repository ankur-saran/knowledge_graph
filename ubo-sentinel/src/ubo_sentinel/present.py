"""What a role is shown of a decision, in the CLI and the review UI: masked, then checked."""

import re
from collections import Counter
from typing import Any

import duckdb

from ubo_sentinel.agents import llm_prose, match_adjudicator
from ubo_sentinel.agents.explainer import render_memo
from ubo_sentinel.agents.guardrail import check_render
from ubo_sentinel.agents.investigator_guardrail import resolve_value
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.graph.store import GraphStore
from ubo_sentinel.graph.views import GraphView, mask_decision, pseudonym_for
from ubo_sentinel.llm.egress import EGRESS_ROLE
from ubo_sentinel.models.change import MonitorRun, SetDiff
from ubo_sentinel.models.decision import Decision
from ubo_sentinel.models.investigation import Finding, Investigation
from ubo_sentinel.models.memo import DecisionView
from ubo_sentinel.models.ontology import OntologyConfig


class RenderWithheld(RuntimeError):
    """The output held a value that is masked for the role. Nothing of it is shown."""


def summary_lines(view: DecisionView, recorded: str = "", resolved_by: str = "") -> list[str]:
    status = f"status {view.status}; {recorded}" if recorded else f"status {view.status}"
    lines = [
        f"Decision {view.decision_id}: {view.recommendation}  ({status})",
        f"Reasons: {', '.join(view.reasons) or 'none'}",
    ]
    if resolved_by:
        lines.append(f"Resolved by: {resolved_by}")
    lines += [
        f"Snapshot set: {view.snapshot_set_id}   Rule pack: {view.rule_pack_id}"
        f"   Depth: {view.max_depth}",
        f"Payload SHA-256: {view.payload_sha256}",
    ]
    if view.final_outcome:
        lines.append(
            f"Reviewed: {view.status}, outcome {view.final_outcome}, by {view.reviewed_by}"
            f" at {view.reviewed_at.isoformat()}"
        )
    lines += [f"GUARDRAIL WARNING: {warning}" for warning in view.guardrail_warnings]
    return lines


def decision_text(
    decision: Decision,
    role: str,
    ontology: OntologyConfig,
    *,
    as_json: bool = False,
    recorded: str = "",
    resolved_by: str = "",
) -> str:
    """The decision as the role may see it. Raises `RenderWithheld` when the guardrail trips."""
    view = mask_decision(decision, role, ontology)
    if as_json:
        text = view.model_dump_json(indent=2)
    else:
        summary = "\n".join(summary_lines(view, recorded, resolved_by))
        text = f"{summary}\n\n{render_memo(view.memo)}"
    if check_render(text, decision, role, ontology):
        raise RenderWithheld(
            f"Decision {view.decision_id} is recorded, but its memo is not shown: the output"
            f" held a value that is masked for role '{role}'."
        )
    return text


def audit_head(log: AuditLog) -> str:
    """A receipt: kept, it later shows whether the end of the log was removed."""
    report = log.verify()
    return f"Audit head: {report.head} (#{report.count})"


# --- the monitor's change digest ------------------------------------------------------


class Masker:
    """Ids as one role may see them, for text that names entities outside any memo.

    A change digest names entities by id, never by name. An entity the role
    sees masked shows as its pseudonym; `withheld()` says whether the text
    still holds anything of it (its id, names, numbers or record id).
    """

    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        set_ids: list[str],
        role: str,
        ontology: OntologyConfig,
    ) -> None:
        hidden = ontology.roles[role].masked_fields
        self._alias: dict[str, str] = {}
        self._values: set[str] = set()
        if not hidden:
            return
        for label, fields in hidden.items():
            columns = ", ".join(f for f in fields if f != "aliases")
            rows = con.execute(
                f"SELECT DISTINCT id, source_record_id, aliases{', ' + columns if columns else ''}"
                " FROM graph_nodes WHERE label = ?"
                " AND snapshot_set_id IN (SELECT unnest(CAST(? AS VARCHAR[])))",
                [label, sorted(set(set_ids))],
            ).fetchall()
            for entity_id, record_id, aliases, *values in rows:
                self._alias[entity_id] = pseudonym_for(entity_id, label)
                self._values.update([entity_id, record_id, *(v for v in values if v)])
                if "aliases" in fields:
                    self._values.update(aliases or [])

    def id(self, entity_id: str | None) -> str:
        return self._alias.get(entity_id or "", entity_id or "")

    def real(self, shown_id: str) -> str:
        """The node id behind a pseudonym of this role's; any other id as it is."""
        return {alias: real for real, alias in self._alias.items()}.get(shown_id, shown_id)

    def text(self, text: str) -> str:
        """Every masked id in the text replaced by its pseudonym, longest first."""
        for entity_id in sorted(self._alias, key=len, reverse=True):
            text = text.replace(entity_id, self._alias[entity_id])
        return text

    def withheld(self, text: str, words: bool = False) -> bool:
        """Whether the text holds a masked value; with `words`, only as a whole word."""
        if not words:
            return any(value in text for value in self._values)
        return any(re.search(rf"(?<!\w){re.escape(value)}(?!\w)", text) for value in self._values)


def guarded(text: str, masker: Masker, what: str, role: str) -> str:
    """The text with masked ids replaced; raises `RenderWithheld` when anything masked is left."""
    text = masker.text(text)
    if masker.withheld(text):
        raise RenderWithheld(f"The {what} is not shown: it held a value masked for role '{role}'.")
    return text


def _kinds(counts: dict[str, int]) -> str:
    return ", ".join(f"{n} {kind.lower()}" for kind, n in counts.items())


def monitor_lines(
    run: MonitorRun, diffs: list[SetDiff], touched: dict[str, list[str]]
) -> list[str]:
    """The change digest and the queue, unmasked: pass the text through `guarded()`."""
    lines = [f"Monitor run {run.run_id} ({run.family}): compared with set {run.curr_set_id}"]
    by_prev = {diff.prev_set_id: diff for diff in diffs}
    for prev, digest in sorted(run.baselines.items()):
        if digest is None:
            lines.append(f"  From set {prev}: not built; its decisions are queued all the same.")
            continue
        diff = by_prev.get(prev)
        changes = diff.changes if diff else []
        lines.append(f"  From set {prev}: {len(changes)} changes (digest {digest[:16]})")
        for cause, kinds in (diff.counts() if diff else {}).items():
            lines.append(f"    {cause:<9} {_kinds(kinds)}")
        for change in changes:
            decisions = touched.get(change.change_id, [])
            fields = f" [{', '.join(change.fields)}]" if change.fields else ""
            reach = (
                f"  -> {len(decisions)} decision(s): {', '.join(decisions)}" if decisions else ""
            )
            lines.append(
                f"    {change.change_id}  {change.cause:<9} {change.domain} {change.kind}{fields}"
                f"  at {', '.join(change.anchor_ids)}{reach}"
            )
    statuses = Counter(item.status for item in run.items)
    summary = ", ".join(f"{n} {status.lower()}" for status, n in sorted(statuses.items()))
    lines.append(f"Re-screen queue: {len(run.items)}{f' ({summary})' if summary else ''}")
    for number, item in enumerate(run.items, 1):
        prior = item.prior_recommendation + (f"/{item.prior_outcome}" if item.prior_outcome else "")
        causes = f" {','.join(item.causes)}" if item.causes else ""
        hops = f" hops {item.hops}" if item.hops is not None else ""
        lines.append(
            f"  {number}. {item.target_id or item.query_norm}  was {prior} ({item.prior_status})"
            f"  {item.reason}{causes}  by {item.basis}{hops}  depth {item.max_depth}"
            f"  prior {item.prior_decision_id}  {item.status}"
        )
    return lines


# --- advisory text beside a decision (Step 13) -------------------------------------------

LABEL = "LLM-assisted, advisory: not part of the decision, checked against the memo by code."


def card_lines(cards: list[dict[str, Any]]) -> list[str]:
    lines = ["Match cards (code, not a model):"]
    for item in cards:
        a, b = item["a"], item["b"]
        lines += [
            f"  {a['id']}  matched to  {b['id']}"
            f"  ({item['match_type']}, {item['match_confidence']})",
            f"    names     {'; '.join(a['names'])}  |  {'; '.join(b['names'])}",
            f"    score     {item['name_score']}  shared tokens: "
            f"{', '.join(item['shared_name_tokens']) or '-'}  alias match: {item['alias_match']}",
            f"    country   {a['jurisdiction'] or '-'}  |  {b['jurisdiction'] or '-'}"
            f"  (agree: {item['jurisdictions_agree']})",
            f"    type      {item['type_a']}  |  {item['type_b']}",
            f"    conflicts LEI: {item['lei_conflict']}  registration: "
            f"{item['registration_conflict']}",
            f"    lists     {', '.join(item['list_sources']) or '-'}",
        ]
    return lines


def _assessment_lines(found: dict[str, Any]) -> list[str]:
    lines = []
    for item in found["output"]["assessments"]:
        lines.append(
            f"  {item['key']}: {item['assessment']} (on {', '.join(item['evidence_fields'])})"
        )
        lines.append(f"    {item['rationale']}")
    for key in found["output"].get("not_sent", []):
        lines.append(f"  {key}: not asked (a masked party: nothing to compare)")
    return lines


def advisory_text(
    found: dict[str, Any], decision: Decision, role: str, ontology: OntologyConfig
) -> list[str]:
    if found["status"] != "ACCEPTED":
        return [f"Not shown: {found['status']} ({'; '.join(found['faults']) or '-'})."]
    if found["feature"] == "prose":
        body = [llm_prose.render_prose(found, decision, role, ontology)]
    else:
        body = _assessment_lines(found)
    return [f"{LABEL} Model {found['model']}, prompt v{found['prompt_version']}.", *body]


def advisory_lines(decision: Decision, role: str, ontology: OntologyConfig) -> list[str]:
    """What sits beside a decision for a reviewer: the match cards (code), and each accepted
    LLM annotation, labelled. Shown by `ubo review` and the review UI."""
    lines: list[str] = []
    cards = match_adjudicator.match_cards(decision, role, ontology)
    if cards:
        lines += card_lines(cards)
    for feature in sorted(decision.llm_annotations):
        found = decision.llm_annotations[feature]
        lines += [f"LLM annotation ({feature}):", *advisory_text(found, decision, role, ontology)]
    return lines


# --- an investigation (Step 14) ------------------------------------------------------------

INVESTIGATION_LABEL = (
    "LLM-assisted, advisory: not part of the decision, checked against what it cites by code."
)


class InvestigationShown:
    """An investigation's entities as one role may see them.

    An investigation is made from the analyst's view: a person is a pseudonym,
    and who it is is not stored. The pseudonym is a hash of the id, so it is
    found again among the set's own nodes, and the entity is then shown through
    this role's view: a reviewer sees the name, an analyst the pseudonym. Text,
    never markup: the names come from public data.
    """

    def __init__(self, store: GraphStore, role: str, ontology: OntologyConfig) -> None:
        self._analyst = Masker(store.con, [store.set_id], EGRESS_ROLE, ontology)
        self.masker = Masker(store.con, [store.set_id], role, ontology)
        self._view = GraphView(store, role, ontology)

    def id(self, analyst_id: str) -> str:
        """The id this role knows the entity by."""
        return self.masker.id(self._analyst.real(analyst_id))

    def entity(self, analyst_id: str) -> str:
        found = self._view.get_entity_by_id(self._analyst.real(analyst_id))
        if found is None:
            return f"[{self.id(analyst_id)}]"
        return f"{found.legal_name} [{found.id}]"

    def screenable(self, analyst_id: str) -> str | None:
        """The id to screen, or None for a party this role sees masked."""
        shown = self.id(analyst_id)
        return None if shown != self._analyst.real(analyst_id) else shown

    def fill(self, text: str, calls: dict[str, Any]) -> str:
        """A finding's slots, filled: an entity as this role sees it, a value as it was read."""

        def slot(match: re.Match[str]) -> str:
            kind, value = match.group(1), match.group(2) or ""
            if kind == "e":
                return self.entity(value)
            call_id, _, pointer = value.partition(".")
            record = calls.get(call_id)
            found = resolve_value(pointer, record.result()) if record else None
            return str(found)

        return llm_prose.SLOT.sub(slot, text)


def _arguments(arguments: dict[str, Any], shown: InvestigationShown) -> str:
    parts = []
    for name, value in arguments.items():
        if isinstance(value, list):
            value = "[" + ", ".join(shown.id(str(item)) for item in value) + "]"
        elif isinstance(value, str):
            value = shown.id(value)
        parts.append(f"{name}={value}")
    return ", ".join(parts)


def investigation_lines(
    investigation: Investigation, shown: InvestigationShown, *, trace: bool = True
) -> list[str]:
    """An investigation as `shown`'s role may see it: pass the text through `guarded()`."""
    calls = {call.call_id: call for call in investigation.calls}
    state = "complete" if investigation.complete else "incomplete"
    model = (
        f"model {investigation.model}, prompt v{investigation.prompt_version}"
        if investigation.model
        else "first pass only: no model was asked"
    )
    lines = [
        f"Investigation {investigation.investigation_id} of decision"
        f" {investigation.decision_id}: {state} ({model})",
        f"Question: {investigation.question}",
        "First pass (code, no model):",
    ]

    def findings(items: list[Finding]) -> list[str]:
        return [
            f"  [{item.code}] {shown.fill(item.text, calls)}  (cites {', '.join(item.cites)})"
            for item in items
        ]

    lines += findings(investigation.first_pass) or ["  Nothing."]
    if investigation.model:
        lines.append(INVESTIGATION_LABEL)
        lines += findings(investigation.findings) or ["  The model added no finding."]
    if investigation.suggested_actions:
        lines.append("Suggested (a person decides):")
    for action in investigation.suggested_actions:
        target = shown.screenable(action.entity_id) if action.entity_id else None
        if action.code == "SCREEN_RELATED_ENTITY" and target:
            lines.append(
                f"  Screen {shown.entity(action.entity_id or '')}: ubo screen --target-id {target}"
                f" --snapshot {investigation.snapshot_set_id}"
                f" --investigation-id {investigation.investigation_id}"
            )
        elif action.code == "SCREEN_RELATED_ENTITY":
            lines.append(
                f"  Screen {shown.entity(action.entity_id or '')}: a party masked for this role"
            )
        else:
            about = f" for {shown.entity(action.entity_id)}" if action.entity_id else ""
            document = f" ({action.document})" if action.document else ""
            lines.append(f"  {action.code}{document}{about}")
    coverage = investigation.coverage
    lines.append(
        f"Looked at: {len(coverage.entities_examined)} entities with"
        f" {', '.join(coverage.tools_called) or 'no tool'}"
    )
    for capped in coverage.capped:
        lines.append(f"  {capped.call_id} {capped.tool}: {capped.left_out} left out by a cap")
    if coverage.refused:
        lines.append("  Refused: " + ", ".join(f"{k} {n}" for k, n in coverage.refused.items()))
    if coverage.subjects_not_examined:
        lines.append(f"  {coverage.subjects_not_examined} parties not looked around (a cap)")
    if investigation.faults:
        lines.append(f"Faults: {'; '.join(investigation.faults)}")
    if investigation.findings_dropped:
        lines.append(f"Findings the guardrail dropped: {investigation.findings_dropped}")
    if investigation.usage:
        usage = investigation.usage
        lines.append(
            f"Usage: {usage.get('steps', 0)} steps, {usage.get('turns', 0)} turns,"
            f" {usage.get('input_tokens', 0)} tokens in, {usage.get('output_tokens', 0)} out"
        )
    if trace:
        lines.append("Trace:")
        for call in investigation.calls:
            left_out = f", {call.truncated} left out" if call.truncated else ""
            what = (
                f"refused: {call.fault}" if call.fault else f"{len(call.items)} item(s){left_out}"
            )
            again = f" (repeats {call.repeat_of})" if call.repeat_of else ""
            asked = _arguments(call.arguments, shown)
            lines.append(f"  {call.call_id} {call.tool}({asked}) -> {what}{again}")
    return lines


def investigation_text(
    investigation: Investigation,
    decision: Decision,
    role: str,
    ontology: OntologyConfig,
    con: duckdb.DuckDBPyConnection | None = None,
    *,
    trace: bool = True,
) -> str:
    """The investigation as the role may see it. Raises `RenderWithheld` when the render
    guardrail trips, on the decision's masked values or on any masked node of the set.
    Raises what opening the set raises."""
    with GraphStore.open(investigation.snapshot_set_id, con) as store:
        shown = InvestigationShown(store, role, ontology)
        text = "\n".join(investigation_lines(investigation, shown, trace=trace))
    # Whole words for the set's nodes: a short alias is the inside of many a company's name.
    if check_render(text, decision, role, ontology) or shown.masker.withheld(text, words=True):
        raise RenderWithheld(
            f"Investigation {investigation.investigation_id} is recorded, but it is not shown:"
            f" the output held a value that is masked for role '{role}'."
        )
    return text
