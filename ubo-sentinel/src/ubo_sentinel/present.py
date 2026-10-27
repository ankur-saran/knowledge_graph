"""What a role is shown of a decision, in the CLI and the review UI: masked, then checked."""

from collections import Counter

import duckdb

from ubo_sentinel.agents.explainer import render_memo
from ubo_sentinel.agents.guardrail import check_render
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.graph.views import mask_decision, pseudonym_for
from ubo_sentinel.models.change import MonitorRun, SetDiff
from ubo_sentinel.models.decision import Decision
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
        self, con: duckdb.DuckDBPyConnection, set_ids: list[str], role: str, ontology: OntologyConfig
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

    def text(self, text: str) -> str:
        """Every masked id in the text replaced by its pseudonym, longest first."""
        for entity_id in sorted(self._alias, key=len, reverse=True):
            text = text.replace(entity_id, self._alias[entity_id])
        return text

    def withheld(self, text: str) -> bool:
        return any(value in text for value in self._values)


def guarded(text: str, masker: Masker, what: str, role: str) -> str:
    """The text with masked ids replaced; raises `RenderWithheld` when anything masked is left."""
    text = masker.text(text)
    if masker.withheld(text):
        raise RenderWithheld(f"The {what} is not shown: it held a value masked for role '{role}'.")
    return text


def _kinds(counts: dict[str, int]) -> str:
    return ", ".join(f"{n} {kind.lower()}" for kind, n in counts.items())


def monitor_lines(run: MonitorRun, diffs: list[SetDiff], touched: dict[str, list[str]]) -> list[str]:
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
            reach = f"  -> {len(decisions)} decision(s): {', '.join(decisions)}" if decisions else ""
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
