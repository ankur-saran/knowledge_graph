"""The review UI: `ubo ui`, or `streamlit run src/ubo_sentinel/ui/app.py` from the repository root.

Glue only. What is screened, reviewed, replayed and shown is decided in
`ui/panels.py` and `ui/graph_viz.py`; this page lays it out. Streamlit runs the
page again on every click, so a screen or a review runs only when its button is
pressed, and what it returned is kept in the session.
"""

import sys
import threading
from pathlib import Path
from typing import Any

import streamlit as st

from ubo_sentinel.agents.audit import DeterminismViolation
from ubo_sentinel.agents.base import ScreenRequest
from ubo_sentinel.agents.explainer import render_memo
from ubo_sentinel.audit.log import GENESIS_HASH, AuditChainError, AuditLog
from ubo_sentinel.audit.replay import UnknownDecision
from ubo_sentinel.audit.review import (
    ReviewError,
    is_superseded,
    pending,
    supersedes_of,
    unscreened,
)
from ubo_sentinel.graph.store import GraphNotBuilt
from ubo_sentinel.graph.views import GraphView, mask_decision
from ubo_sentinel.models.decision import Decision
from ubo_sentinel.models.ontology import OntologyConfig, load_ontology, ontology_path
from ubo_sentinel.pipeline.bronze import UnknownSnapshotSet
from ubo_sentinel.present import summary_lines
from ubo_sentinel.rules.pack import DEFAULT_PACK
from ubo_sentinel.ui import graph_viz, panels

PANEL_TITLES = {"screen": "Screen", "review": "Review", "audit": "Audit", "build": "Build info"}


@st.cache_resource
def _ontology(path: str) -> OntologyConfig:
    return load_ontology(Path(path))


@st.cache_resource
def _write_lock() -> threading.Lock:
    """Sessions are threads of one process: one screen or review at a time, on top of the
    log's own file lock, which keeps other processes (the CLI) in turn."""
    return threading.Lock()


@st.cache_data(max_entries=4, show_spinner=False)
def _log_contents(root: str, size: int, mtime_ns: int) -> tuple[list, list, list, list]:
    """The log read and verified once per change of the file, not on every click."""
    log = AuditLog(Path(root))
    return log.records(), log.decisions(), pending(log), unscreened(log)


def log_contents(log: AuditLog) -> tuple[list, list, list, list]:
    try:
        stat = log.path.stat()
        key = (stat.st_size, stat.st_mtime_ns)
    except FileNotFoundError:
        key = (0, 0)
    return _log_contents(str(log.path.parent.parent), *key)


def startup_role(roles: list[str]) -> str:
    """`ubo ui --role R` passes the role after `--`."""
    args = sys.argv[1:]
    if "--role" in args and args.index("--role") + 1 < len(args):
        role = args[args.index("--role") + 1]
        if role in roles:
            return role
    return "analyst" if "analyst" in roles else roles[0]


def head_receipt(records: list[dict[str, Any]]) -> str:
    head = records[-1]["hash"] if records else GENESIS_HASH
    return f"Audit head: {head} (#{len(records)})"


# --- one decision ------------------------------------------------------------------------------


def show_decision(
    decision: Decision,
    role: str,
    ontology: OntologyConfig,
    records: list[dict[str, Any]],
    decisions: list[Decision],
    *,
    context: str,
    recorded: str = "",
    resolved_by: str = "",
) -> bool:
    """Show a decision as the role may see it. False when the render guardrail withheld it."""
    view = mask_decision(decision, role, ontology)
    summary = "\n".join(summary_lines(view, recorded, resolved_by))
    memo_text = render_memo(view.memo)
    if panels.guarded(summary + memo_text, decision, role, ontology) is None:
        st.error(panels.WITHHELD)
        return False

    colour = {"ESCALATE": "red", "REVIEW": "orange", "CLEAR": "green"}[view.recommendation]
    st.markdown(f"### :{colour}[{view.recommendation}]  ·  `{view.decision_id}`")
    st.code(summary, language=None)
    st.caption(head_receipt(records))
    notice = panels.licence_notice(view)
    if notice:
        st.info(notice)

    st.markdown("#### Evidence graph")
    scope = st.radio(
        "Show",
        ["paths", "checked"],
        format_func={"paths": "Evidence paths and gaps", "checked": "Everything checked"}.get,
        horizontal=True,
        key=f"{context}-scope",
    )
    spec = graph_viz.evidence_graph(view, scope=scope)
    if panels.guarded(spec.text(), decision, role, ontology) is None:
        st.error(panels.WITHHELD)
    elif spec.nodes:
        # The page runs script; the names in it are JSON that cannot end a script element.
        st.iframe(graph_viz.to_html(spec), height=640)
        for note in spec.notes:
            st.caption(note)
        with st.expander("Legend"):
            st.table([{"mark": mark, "means": meaning} for mark, meaning in graph_viz.LEGEND])
        with st.expander("The graph as a table"):
            st.dataframe(graph_viz.edge_rows(spec), hide_index=True)
        evidence_drilldown(decision, role, ontology, spec, context)
    else:
        for note in spec.notes:
            st.caption(note)

    choice = panels.what_if_choice(decision)
    if choice:
        st.markdown("#### What would settle this review")
        st.write(
            f"If the contested match is the listed party: **{choice.if_confirmed}**. "
            f"If it is someone else: **{choice.if_rejected}**."
        )
        if not choice.decisive:
            st.caption("The match does not decide the outcome either way.")

    advisory = panels.advisory(decision, role, ontology)
    if advisory:
        with st.expander("Match cards and LLM-assisted notes (advisory)"):
            st.text(advisory)

    with st.expander("Memo", expanded=True):
        st.markdown(memo_text)
    words = panels.glossary(view.memo)
    codes = sorted(set(view.reasons) | {gap.code for gap in view.data_gaps})
    if codes:
        with st.expander("What the codes mean"):
            for code in codes:
                st.markdown(f"- `{code}`: {words.get(code, '')}")
    trace = panels.trace_rows(records, decision.decision_id)
    if trace:
        with st.expander("How this was computed"):
            st.dataframe(trace, hide_index=True)
    history = panels.history_rows(decisions, decision, role, ontology)
    if history and panels.guarded(repr(history), decision, role, ontology) is not None:
        with st.expander(f"Other decisions on this entity ({len(history)})"):
            st.dataframe(history, hide_index=True)
    return True


def evidence_drilldown(
    decision: Decision,
    role: str,
    ontology: OntologyConfig,
    spec: graph_viz.GraphSpec,
    context: str,
) -> None:
    """A node's claims, what they cite, and the published record behind a citation."""
    with st.expander("Evidence behind a node"):
        labels = {node.id: node.label.replace("\n", " ") for node in spec.nodes}
        node_id = st.selectbox("Node", list(labels), format_func=labels.get, key=f"{context}-node")
        found = panels.node_evidence(decision, node_id)
        if not found:
            st.caption("The memo makes no claim about this node.")
            return
        rows = [{"claim": e.claim, "cites": e.citation} for e in found]
        if panels.guarded(repr(rows), decision, role, ontology) is None:
            st.error(panels.WITHHELD)
            return
        st.dataframe(rows, hide_index=True)
        cited = [e for e in found if e.provenance is not None]
        if not cited:
            return
        number = st.selectbox(
            "Citation",
            range(len(cited)),
            format_func=lambda i: f"{cited[i].claim}: {cited[i].citation}",
            key=f"{context}-citation",
        )
        if st.button("Open the source record", key=f"{context}-open"):
            try:
                record = panels.source_record(role, ontology, decision, cited[number].provenance)
            except (UnknownSnapshotSet, GraphNotBuilt) as exc:
                st.error(f"The decision's snapshot set cannot be read here: {exc}")
                return
            if record is None:
                st.warning("No such record in this database.")
            elif panels.guarded(repr(record), decision, role, ontology) is None:
                st.error(panels.WITHHELD)
            else:
                st.json(record)


# --- panels ------------------------------------------------------------------------------------


def run_screen(request: ScreenRequest, log: AuditLog, ontology: OntologyConfig) -> None:
    with _write_lock():
        st.session_state["screen_result"] = panels.screen(request, log, ontology)
    st.session_state["screen_request"] = request


def screen_panel(role, actor, snapshot, rule_pack, log, ontology, records, decisions) -> None:
    with st.form("screen-form"):
        query = st.text_input("Entity name or LEI", key="query")
        left, right = st.columns(2)
        lei = left.text_input("LEI", key="lei")
        registration_number = right.text_input("Registration number", key="regno")
        jurisdiction = left.text_input("Jurisdiction (ranks, never filters)", key="jurisdiction")
        depth = right.number_input(
            "Depth (0: the rule pack's)", min_value=0, max_value=20, value=0, key="depth"
        )
        submitted = st.form_submit_button("Screen", key="screen-submit")
    if submitted:
        if not (query.strip() or lei.strip() or registration_number.strip()):
            st.warning("Give a name or an LEI to screen.")
        elif not actor.strip():
            st.warning("Give an actor: every screen is recorded with who made it.")
        else:
            run_screen(
                ScreenRequest(
                    query=query.strip(),
                    lei=lei.strip() or None,
                    registration_number=registration_number.strip() or None,
                    jurisdiction=jurisdiction.strip() or None,
                    snapshot=snapshot,
                    rule_pack=rule_pack,
                    max_depth=int(depth) or None,
                    role=role,
                    actor=actor.strip(),
                ),
                log,
                ontology,
            )
            records, decisions, _, _ = log_contents(log)

    result: panels.ScreenResult | None = st.session_state.get("screen_result")
    request: ScreenRequest | None = st.session_state.get("screen_request")
    if result is None or request is None:
        return
    if request.role != role:
        st.caption("The last screen was made under another role; screen again to see it here.")
        return
    if result.kind in ("error", "failed"):
        st.error(result.message)
        return
    if result.kind == "pick":
        st.info(result.message)
        rows = panels.candidate_rows(result.candidates, role, ontology)
        if rows is None:
            st.error(panels.WITHHELD)
            return
        st.dataframe(rows, hide_index=True)
        pick = st.radio(
            "Screen which one",
            [row["#"] for row in rows],
            format_func=lambda n: f"{n}. {rows[n - 1]['name']} [{rows[n - 1]['jurisdiction']}]",
            key="pick",
        )
        if st.button("Screen this one", key="pick-submit"):
            run_screen(request.model_copy(update={"pick": pick}), log, ontology)
            st.rerun()
        return

    decision = result.decision
    recorded = "recorded" if result.is_new else "already on record, unchanged"
    shown = show_decision(
        decision,
        role,
        ontology,
        records,
        decisions,
        context="screen",
        recorded=recorded,
        resolved_by=result.resolved_by,
    )
    follow = panels.follow_ups(decision, role, ontology) if shown else []
    if follow:
        st.markdown("#### Next actions")
        for number, item in enumerate(follow):
            if st.button(item.label, key=f"follow-{number}"):
                run_screen(request.model_copy(update=item.update), log, ontology)
                st.rerun()


def rescreen_section(role, actor, log, ontology, records, decisions) -> None:
    """The latest monitor run's queue (`ubo monitor`), and one item screened again."""
    run = panels.latest_run(records)
    if run is None:
        return
    run = panels.run_with_statuses(run, log)
    st.markdown(f"#### Re-screen queue ({len(run.items)}), monitor run {run.run_id}")
    st.caption(
        f"Decisions that data newer than their own may change, against set {run.curr_set_id}."
        " A re-screen is an ordinary decision: it waits for a reviewer other than its screener."
    )
    if not run.items:
        return
    st.dataframe(panels.rescreen_rows(run, decisions, role, ontology), hide_index=True)
    pending_ids = [item.prior_decision_id for item in run.items if item.status == "PENDING"]
    if not pending_ids:
        return
    chosen = st.selectbox("Re-screen the entity of", pending_ids, key="rescreen-item")
    if st.button("Re-screen", disabled=not actor.strip(), key="rescreen-submit"):
        try:
            with _write_lock():
                result = panels.rescreen_item(
                    run, chosen, log, ontology, role=role, actor=actor.strip()
                )
        except (PermissionError, AuditChainError, DeterminismViolation) as exc:
            st.error(str(exc))
            return
        if result.decision is None:
            st.warning(f"Not screened ({result.outcome}): screen it from the Screen panel.")
            return
        records, _, _, _ = log_contents(log)
        st.session_state["review_flash"] = (
            f"Re-screened: decision {result.decision.decision_id},"
            f" {result.decision.recommendation} ({result.transition}). {head_receipt(records)}"
        )
        st.rerun()


def review_panel(role, actor, log, ontology, records, decisions, waiting, failed) -> None:
    flash = st.session_state.pop("review_flash", None)
    if flash:
        st.success(flash)
    st.markdown(f"#### Waiting for a review ({len(waiting)})")
    if waiting:
        follows = supersedes_of(log)
        st.dataframe(panels.queue_rows(waiting, role, ontology, follows=follows), hide_index=True)
    if failed:
        st.markdown(f"#### Screens that failed and were not run again ({len(failed)})")
        st.caption("Each is an entity nobody screened.")
        st.dataframe(panels.unscreened_rows(failed), hide_index=True)
    rescreen_section(role, actor, log, ontology, records, decisions)

    ids = [d.decision_id for d in waiting] + [
        d.decision_id for d in reversed(decisions) if d.status != "RECOMMENDED"
    ]
    if not ids:
        st.caption("No decisions on record.")
        return
    by_id = {d.decision_id: d for d in decisions}
    chosen = st.selectbox(
        "Decision",
        ids,
        format_func=lambda i: f"{i}  {by_id[i].recommendation}  {by_id[i].status}",
        key="review-decision",
    )
    decision = by_id[chosen]
    if not show_decision(decision, role, ontology, records, decisions, context="review"):
        return  # Nothing is decided on a memo that could not be shown.
    if decision.status != "RECOMMENDED":
        st.info(f"Reviewed: {decision.status}, outcome {decision.final_outcome}.")
        return

    st.markdown("#### Your review")
    superseded = is_superseded(decision)
    if superseded:
        st.warning("Newer data of this snapshot family exists: this decision may be stale.")
    choice = panels.what_if_choice(decision)
    matches = None
    if choice:
        ruling = st.radio(
            "Settle the contested match",
            ["", "confirm", "reject"],
            format_func={
                "": "Not settled: choose the outcome myself",
                "confirm": f"It is the listed party → {choice.if_confirmed}",
                "reject": f"It is someone else → {choice.if_rejected}",
            }.get,
            key="ruling",
        )
        matches = ruling or None
    action = st.radio(
        "Action",
        ["approve", "override", "escalate"],
        horizontal=True,
        key="action",
    )
    outcome = None
    if action == "approve" and decision.recommendation == "REVIEW" and matches is None:
        options = ["ESCALATE", "CLEAR"] if panels.clearable(decision) else ["ESCALATE"]
        outcome = st.radio("Outcome", options, horizontal=True, key="outcome")
    reason = st.text_area("Reason", key="reason")
    accept_stale = (
        st.checkbox("Clear on the older data all the same", key="stale") if superseded else False
    )
    problem = panels.review_problem(
        decision,
        action,
        actor=actor,
        screener=panels.screened_by(records, decision.decision_id),
        outcome=outcome,
        reason=reason,
        matches=matches,
        superseded=superseded,
        accept_stale=accept_stale,
    )
    if problem:
        st.warning(problem)
    if st.button("Record the review", disabled=problem is not None, key="review-submit"):
        try:
            with _write_lock():
                reviewed = panels.do_review(
                    log,
                    ontology,
                    decision,
                    action=action,
                    actor=actor.strip(),
                    role=role,
                    outcome=outcome,
                    reason=reason,
                    matches=matches,
                    accept_stale=accept_stale,
                )
        except ReviewError as exc:
            st.warning(str(exc))
            return
        except (PermissionError, AuditChainError) as exc:
            st.error(str(exc))
            return
        records, _, _, _ = log_contents(log)
        st.session_state["review_flash"] = (
            f"Decision {reviewed.decision_id}: {reviewed.status}, outcome"
            f" {reviewed.final_outcome}. {head_receipt(records)}"
        )
        st.rerun()


def audit_panel(role, log, ontology, records, decisions) -> None:
    if panels.may(ontology, role, "audit-verify"):
        st.markdown("#### Verify the log")
        expect = st.text_input("Expected head (from a receipt kept elsewhere)", key="expect")
        if st.button("Verify", key="verify"):
            result = panels.verify(log, ontology, role, expect)
            if result.intact:
                st.success(f"Chain intact ({result.report.count} records)")
            else:
                st.error(f"Chain BROKEN: {'; '.join(result.problems)}")
            st.code(f"Head: {result.report.head}", language=None)
    if not panels.may(ontology, role, "audit-replay") or not decisions:
        return
    st.markdown("#### Replay a decision")
    by_id = {d.decision_id: d for d in decisions}
    chosen = st.selectbox(
        "Decision",
        [d.decision_id for d in reversed(decisions)],
        format_func=lambda i: f"{i}  {by_id[i].recommendation}  {by_id[i].status}",
        key="audit-decision",
    )
    deep = st.checkbox("Recompute every digest from the rows (--deep)", key="deep")
    if st.button("Replay", key="replay"):
        try:
            report = panels.replay(log, ontology, role, chosen, deep=deep)
        except (UnknownDecision, AuditChainError, PermissionError) as exc:
            st.error(str(exc))
        else:
            text = f"{report.verdict}: {report.detail}"
            if report.identical:
                st.success("Replay identical")
            else:
                st.error(text)
            if report.differing_fields:
                st.write("Fields that differ: " + ", ".join(report.differing_fields))
            st.caption(f"Evidence read from the {report.evidence_source}.")
    with st.expander("The decision (read-only)"):
        show_decision(by_id[chosen], role, ontology, records, decisions, context="audit")


def build_panel(role, snapshot, ontology) -> None:
    try:
        with GraphView.open(role, snapshot, ontology) as view:
            st.json(view.build_info())
    except (UnknownSnapshotSet, GraphNotBuilt) as exc:
        st.error(str(exc))


# --- the page ----------------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="ubo-sentinel review", layout="wide")
    try:
        ontology = _ontology(str(ontology_path().resolve()))
    except OSError as exc:
        st.error(f"Cannot read the ontology: {exc}. Run the UI from the repository root.")
        return
    roles = list(ontology.roles)
    st.session_state.setdefault("role", startup_role(roles))
    st.session_state.setdefault("actor", st.session_state["role"])

    with st.sidebar:
        st.title("ubo-sentinel")
        role = st.selectbox("Role", roles, key="role")
        actor = st.text_input("Actor", key="actor")
        st.caption("Simulated roles and identity: choosing them is not authentication.")
        snapshot = st.text_input("Snapshot set", value="fixtures", key="snapshot")
        rule_pack = st.text_input("Rule pack", value=DEFAULT_PACK, key="rule_pack")
        st.caption(
            "Not a production sanctions-screening system and not legal advice."
            " Rule packs must be validated by compliance counsel."
        )

    log = AuditLog()
    try:
        records, decisions, waiting, failed = log_contents(log)
    except AuditChainError as exc:
        st.error(f"The audit log does not verify; nothing can be shown or recorded: {exc}")
        return
    if not panels.may_read_decisions(ontology, role):
        decisions, waiting, failed = [], [], []

    shown = panels.allowed_panels(ontology, role)
    if not shown:
        st.warning(f"Role '{role}' may use no panel of this UI.")
        return
    for tab, panel in zip(st.tabs([PANEL_TITLES[p] for p in shown]), shown, strict=True):
        with tab:
            if panel == "screen":
                screen_panel(role, actor, snapshot, rule_pack, log, ontology, records, decisions)
            elif panel == "review":
                review_panel(role, actor, log, ontology, records, decisions, waiting, failed)
            elif panel == "audit":
                audit_panel(role, log, ontology, records, decisions)
            else:
                build_panel(role, snapshot, ontology)


main()
