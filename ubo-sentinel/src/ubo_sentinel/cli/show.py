"""What the commands print of a decision: always a role's view, checked before it is shown."""

import sys

from ubo_sentinel.agents.explainer import render_memo
from ubo_sentinel.agents.guardrail import check_render
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.graph.views import mask_decision
from ubo_sentinel.models.decision import Decision
from ubo_sentinel.models.memo import DecisionView
from ubo_sentinel.models.ontology import OntologyConfig


class RenderWithheld(RuntimeError):
    """The output held a value that is masked for the role. Nothing of it is shown."""


def utf8_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        # A non-Latin name must not fail a piped or redirected run on Windows.
        sys.stdout.reconfigure(encoding="utf-8")


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
