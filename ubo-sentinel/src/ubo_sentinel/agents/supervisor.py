"""Supervisor: runs the agents in order. A state machine, no model.

    Intake → Traversal → SanctionsMatch → RuleEngine → Explainer → Guardrail → Audit

A screen ends in one of three ways. `NeedsDisambiguation`: the query has
several candidates and the caller runs it again with a pick. `Recommended`: a
decision in status RECOMMENDED, waiting for a reviewer. `ScreenFailed`: an
agent raised; nothing was concluded and no decision is on record, which a
caller must treat as "not screened", never as clear.
"""

import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import duckdb

from ubo_sentinel.agents.audit import AuditAgent, DeterminismViolation, decision_id_of
from ubo_sentinel.agents.base import (
    BaseAgent,
    ContractError,
    PipelineState,
    Resolution,
    ScreenContext,
    ScreenRequest,
    ScreenUsageError,
    check_contract,
)
from ubo_sentinel.agents.explainer import ExplainerAgent
from ubo_sentinel.agents.guardrail import GuardrailAgent
from ubo_sentinel.agents.intake import IntakeAgent
from ubo_sentinel.agents.rule_engine import RuleEngineAgent
from ubo_sentinel.agents.sanctions_match import SanctionsMatchAgent
from ubo_sentinel.agents.traversal import TraversalAgent
from ubo_sentinel.audit.log import AuditChainError, AuditLog
from ubo_sentinel.graph.store import GraphStore
from ubo_sentinel.models.canonical import sha256_hex
from ubo_sentinel.models.decision import Decision
from ubo_sentinel.models.evidence import ReasonCode
from ubo_sentinel.models.graph import EntityMatch
from ubo_sentinel.models.ontology import OntologyConfig
from ubo_sentinel.rules.pack import RulePack, load_rule_pack, pack_path

PIPELINE: tuple[type[BaseAgent], ...] = (
    IntakeAgent,
    TraversalAgent,
    SanctionsMatchAgent,
    RuleEngineAgent,
    ExplainerAgent,
    GuardrailAgent,
    AuditAgent,
)

# Raised to the caller as they are: the request or the installation is at
# fault, not the screen, and a broken log or a broken promise of determinism
# must stop the caller.
_NOT_A_SCREEN_FAILURE = (
    ScreenUsageError,
    DeterminismViolation,
    AuditChainError,
    PermissionError,
)


@dataclass(frozen=True)
class NeedsDisambiguation:
    """Several entities answer to the query; run it again with `pick` or `target_id`."""

    resolution: Resolution

    @property
    def candidates(self) -> list[EntityMatch]:
        return self.resolution.candidates


@dataclass(frozen=True)
class Recommended:
    decision: Decision
    resolution: Resolution
    # False when the same inputs were screened before and the recorded decision is returned.
    is_new: bool
    trace: list[dict[str, Any]] = field(default_factory=list)

    @property
    def payload_sha256(self) -> str:
        return sha256_hex(self.decision.canonical_payload())


@dataclass(frozen=True)
class ScreenFailed:
    """An agent raised. Shown as REVIEW; no decision is recorded, so nothing is cached."""

    agent: str
    error: str
    recommendation: str = "REVIEW"
    reason: ReasonCode = "PIPELINE_ERROR"


Outcome = NeedsDisambiguation | Recommended | ScreenFailed


def utc_now() -> datetime:
    return datetime.now(UTC)


class Supervisor:
    def __init__(
        self,
        ontology: OntologyConfig,
        audit: AuditLog | None = None,
        now: Callable[[], datetime] = utc_now,
        agents: tuple[type[BaseAgent], ...] = PIPELINE,
    ) -> None:
        self.ontology = ontology
        self.audit = audit
        self.now = now
        self.agents = [agent() for agent in agents]

    def run(
        self,
        request: ScreenRequest,
        con: duckdb.DuckDBPyConnection | None = None,
        pack: RulePack | None = None,
    ) -> Outcome:
        """Screen one query against one snapshot set.

        Raises `UnknownRulePack`, `UnknownSnapshotSet` or `GraphNotBuilt` when
        there is nothing to screen against, `ScreenUsageError` for a request
        that cannot be run, `DeterminismViolation` and `AuditChainError`.
        """
        pack = pack or load_rule_pack(pack_path(request.rule_pack))
        with GraphStore.open(request.snapshot, con) as store:
            ctx = ScreenContext(
                store=store, pack=pack, ontology=self.ontology, now=self.now, audit=self.audit
            )
            return self._run(PipelineState(request=request), ctx)

    def _run(self, state: PipelineState, ctx: ScreenContext) -> Outcome:
        for agent in self.agents:
            if state.halt == "NEEDS_DISAMBIGUATION":
                return NeedsDisambiguation(state.resolution)
            if state.halt is not None and not agent.runs_after_halt:
                continue
            first_call, started = len(ctx.store.calls), time.perf_counter()
            try:
                check_contract(agent, state, before=True)
                state = agent.run(state, ctx)
                if state.halt is None or agent.runs_after_halt:
                    check_contract(agent, state, before=False)
            except _NOT_A_SCREEN_FAILURE:
                raise
            except Exception as exc:  # noqa: BLE001 - fail closed: any fault is "not screened"
                return self._failed(agent, exc, state, ctx)
            ctx.trace.append(
                {
                    "agent": agent.name,
                    "seconds": round(time.perf_counter() - started, 6),
                    "queries": [
                        {"name": call.name, "rows": call.rows, "seconds": round(call.seconds, 6)}
                        for call in ctx.store.calls[first_call:]
                    ],
                }
            )
        if state.halt == "NEEDS_DISAMBIGUATION":
            return NeedsDisambiguation(state.resolution)
        if state.decision is None:
            raise ContractError("the pipeline ended without a decision")
        return Recommended(state.decision, state.resolution, ctx.is_new, ctx.trace)

    def _failed(
        self, agent: BaseAgent, exc: Exception, state: PipelineState, ctx: ScreenContext
    ) -> ScreenFailed:
        failed = ScreenFailed(agent=agent.name, error=f"{type(exc).__name__}: {exc}")
        if self.audit is not None:
            self.audit.append(
                ontology=self.ontology,
                decision_id=decision_id_of(state, ctx),
                event_type="SCREEN_FAILED",
                actor=state.request.actor,
                role=state.request.role,
                created_at=self.now(),
                note=failed.error,
                payload={
                    "agent": agent.name,
                    "request": state.request.model_dump(mode="json"),
                    "traceback": traceback.format_exception(exc),
                    "trace": ctx.trace,
                },
            )
        return failed
