"""What every agent shares: the request, the pipeline state, the context and the contract.

An agent is one step of a screen. It reads the fields it `requires` from the
state and returns the state with the fields it `provides` filled. The
supervisor checks both, so a step that is run out of order fails at its own
boundary. Graph data is read through `graph/queries.py` only, unmasked: a
decision does not depend on who asked for it.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.graph.store import GraphStore
from ubo_sentinel.models.decision import Decision
from ubo_sentinel.models.graph import EntityMatch, SanctionMatch, SubGraph
from ubo_sentinel.models.memo import Memo, WhatIf
from ubo_sentinel.models.ontology import OntologyConfig, Role
from ubo_sentinel.models.rule_result import RuleResult
from ubo_sentinel.pipeline import bronze
from ubo_sentinel.rules.engine import RuleEngine
from ubo_sentinel.rules.pack import DEFAULT_PACK, RulePack

# Raise when a change to the agents alters the decision a screen produces: how
# Intake selects, what the memo holds, what the guardrail checks. It is part of
# every `decision_id`.
PIPELINE_VERSION = 1

# How Intake arrived at the target.
ResolutionMethod = Literal["TARGET_ID", "IDENTIFIER", "SOLE_NAME", "ANALYST_PICK", "NOT_FOUND"]

# Why the steps between Intake and the memo were not all run.
Halt = Literal["NEEDS_DISAMBIGUATION", "ENTITY_NOT_FOUND", "EVIDENCE_LIMIT_EXCEEDED"]


class ScreenUsageError(ValueError):
    """The request cannot be run as given: an unknown target id, a pick that is out of range."""


class ContractError(RuntimeError):
    """An agent was run without what it requires, or did not provide what it declares."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ScreenRequest(_Model):
    """What the caller asked for, as typed. None of it is part of the canonical payload."""

    query: str = ""
    lei: str | None = None
    registration_number: str | None = None
    jurisdiction: str | None = None
    # Screen this entity; no resolution.
    target_id: str | None = None
    # The analyst's choice among the candidates of an ambiguous query, from 1.
    pick: int | None = Field(default=None, ge=1)
    snapshot: str = bronze.ALIAS_FIXTURES
    rule_pack: str = DEFAULT_PACK
    # Overrides the rule pack's `max_depth`.
    max_depth: int | None = Field(default=None, ge=1)
    role: Role = "analyst"
    actor: str = "analyst"


class Resolution(_Model):
    """How the query became a target: what was found, what was chosen, and by whom."""

    method: ResolutionMethod
    target_id: str | None = None
    # The candidates an analyst is shown, best first, one per entity.
    candidates: list[EntityMatch] = []
    # Candidate -> the linked records of the same entity that were folded into it.
    folded: dict[str, list[str]] = {}


class PipelineState(_Model):
    request: ScreenRequest
    resolution: Resolution | None = None
    target_id: str | None = None
    subgraph: SubGraph | None = None
    sanction_matches: list[SanctionMatch] | None = None
    rule_result: RuleResult | None = None
    what_if: WhatIf | None = None
    memo: Memo | None = None
    guardrail_warnings: list[str] | None = None
    decision: Decision | None = None
    halt: Halt | None = None

    def with_(self, **fields: Any) -> "PipelineState":
        return self.model_copy(update=fields)


@dataclass
class ScreenContext:
    """What a screen runs against. One store, so one snapshot set."""

    store: GraphStore
    pack: RulePack
    ontology: OntologyConfig
    now: Callable[[], datetime]
    # None: the screen is computed and nothing is recorded (`ubo eval`).
    audit: AuditLog | None = None
    engine: RuleEngine = field(init=False)
    # What each agent took, for the audit event. Never part of a decision.
    trace: list[dict[str, Any]] = field(default_factory=list)
    # Set by the Audit Agent.
    is_new: bool = True

    def __post_init__(self) -> None:
        self.engine = RuleEngine(self.pack)


class BaseAgent:
    name: ClassVar[str]
    # Fields of `PipelineState` that must be set before `run`, and are set after it.
    requires: ClassVar[tuple[str, ...]] = ()
    provides: ClassVar[tuple[str, ...]] = ()
    # True for the agents that still run when an earlier one halted the screen.
    runs_after_halt: ClassVar[bool] = False

    def run(self, state: PipelineState, ctx: ScreenContext) -> PipelineState:
        raise NotImplementedError


def check_contract(agent: BaseAgent, state: PipelineState, *, before: bool) -> None:
    """Raise `ContractError` unless the state holds what the agent requires or provides."""
    fields = agent.requires if before else agent.provides
    missing = [name for name in fields if getattr(state, name) is None]
    if missing:
        side = "requires" if before else "did not provide"
        raise ContractError(f"{agent.name} {side}: {', '.join(missing)}")
