"""Audit Agent: the state becomes a decision, and the decision an audit event.

Screening the same inputs again gives the same `decision_id`. The decision is
then computed all the same and compared with the one on record: equal, and the
recorded one is returned and nothing is written; different, and the screen
stops, because one id must never stand for two decisions.
"""

from typing import Any

from ubo_sentinel import __version__
from ubo_sentinel.agents.base import PIPELINE_VERSION, BaseAgent, PipelineState, ScreenContext
from ubo_sentinel.agents.traversal import screen_depth
from ubo_sentinel.audit.log import decision_of
from ubo_sentinel.models.canonical import sha256_hex
from ubo_sentinel.models.decision import Decision, Recommendation, compute_decision_id

SEVERITY: dict[Recommendation, int] = {"CLEAR": 0, "REVIEW": 1, "ESCALATE": 2}


class DeterminismViolation(RuntimeError):
    """The same inputs gave a decision that differs from the one on record."""


class _OnRecord(Exception):
    """Raised by the append's guard: the decision id already has a recommendation."""


def at_least_review(recommendation: Recommendation) -> Recommendation:
    """A failure raises a recommendation to REVIEW; it never lowers an ESCALATE."""
    return max(recommendation, "REVIEW", key=SEVERITY.__getitem__)


def decision_id_of(state: PipelineState, ctx: ScreenContext) -> str:
    return compute_decision_id(
        state.target_id,
        state.request.query,
        dict(ctx.store.snapshot_set.snapshot_ids),
        ctx.pack.rule_pack_hash,
        __version__,
        screen_depth(state, ctx),
        ctx.store.gold_digest,
        PIPELINE_VERSION,
        state.request.identifiers(),
    )


def assemble(state: PipelineState, ctx: ScreenContext) -> Decision:
    """The decision a finished screen stands for. Reads no clock but `ctx.now`."""
    memo, result, warnings = state.memo, state.rule_result, state.guardrail_warnings
    recommendation, reasons = memo.recommendation, list(memo.reasons)
    if warnings:
        recommendation = at_least_review(recommendation)
        reasons.append("GUARDRAIL_FAILED")
    return Decision(
        decision_id=decision_id_of(state, ctx),
        query=state.request.query,
        query_identifiers=state.request.identifiers(),
        target_id=state.target_id,
        recommendation=recommendation,
        status="RECOMMENDED",
        reasons=reasons,
        blocked_set=result.blocked_set if result else [],
        possibly_blocked_set=result.possibly_blocked_set if result else [],
        paths=result.paths if result else [],
        effective_exposure=memo.effective_exposure,
        data_gaps=result.data_gaps if result else [],
        guardrail_warnings=warnings,
        memo_json=memo.model_dump(mode="json"),
        snapshot_set_id=ctx.store.set_id,
        snapshot_ids=dict(ctx.store.snapshot_set.snapshot_ids),
        rule_pack_id=ctx.pack.id,
        rule_pack_hash=ctx.pack.rule_pack_hash,
        engine_version=__version__,
        pipeline_version=PIPELINE_VERSION,
        graph_digest=ctx.store.gold_digest,
        max_depth=screen_depth(state, ctx),
        created_at=ctx.now(),
    )


def differing_fields(one: Decision, other: Decision) -> list[str]:
    a, b = one.canonical_payload(), other.canonical_payload()
    return sorted(name for name in a if a[name] != b.get(name))


class AuditAgent(BaseAgent):
    name = "Audit"
    requires = ("resolution", "memo", "guardrail_warnings")
    provides = ("decision",)
    runs_after_halt = True

    def run(self, state: PipelineState, ctx: ScreenContext) -> PipelineState:
        decision = assemble(state, ctx)
        if ctx.audit is None:
            return state.with_(decision=decision)

        on_record: list[Decision] = []

        def not_on_record(records: list[dict[str, Any]]) -> None:
            # Inside the log's lock, on the one read the append makes: the
            # lookup and the write see the same log, and a screen reads it once.
            recorded = decision_of(records, decision.decision_id)
            if recorded is not None:
                on_record.append(recorded)
                raise _OnRecord

        payload: dict[str, Any] = {
            "decision": decision.model_dump(mode="json"),
            "payload_sha256": sha256_hex(decision.canonical_payload()),
            # Everything replay needs that the decision does not hold: the pack's
            # values (its file may change), and how the query became the target.
            "rule_pack": ctx.pack.model_dump(mode="json"),
            "request": state.request.model_dump(mode="json"),
            "resolution": state.resolution.model_dump(mode="json"),
            # What the screen read, as digests: replay compares the database with
            # these, which the chain protects, and not with the database's own registry.
            "evidence": ctx.store.evidence_info(),
            "trace": ctx.trace,
        }
        try:
            ctx.audit.append(
                ontology=ctx.ontology,
                decision_id=decision.decision_id,
                event_type="RECOMMENDED",
                actor=state.request.actor,
                role=state.request.role,
                created_at=decision.created_at,
                payload=payload,
                guard=not_on_record,
            )
        except _OnRecord:
            recorded = on_record[0]
            fields = differing_fields(decision, recorded)
            if fields:
                raise DeterminismViolation(
                    f"Decision {decision.decision_id} is on record with a different"
                    f" {', '.join(fields)}. The same inputs must give the same decision:"
                    " code that changes a result must raise PIPELINE_VERSION, RULES_VERSION"
                    " or TRANSFORM_VERSION."
                ) from None
            ctx.is_new = False
            return state.with_(decision=recorded)
        return state.with_(decision=decision)
