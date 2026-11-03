"""What an investigation holds (Step 14): typed findings, closed actions, each tool call."""

from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict

from ubo_sentinel.models.audit_event import RUN_PREFIX
from ubo_sentinel.models.memo import ActionCode
from ubo_sentinel.models.provenance import NonEmptyStr

# What a finding says. A closed set, so a finding is scored by its code and its
# subjects, and no text of a model's decides what kind of claim it is.
FindingCode = Literal[
    "SHARED_ADDRESS",
    "COMMON_CONTROLLER",
    "DESIGNATED_SIBLING",
    "PATH_BETWEEN",
    "HISTORY_CHANGE",
    "LINKED_RECORD_OWNERS",
    "NO_FURTHER_LINK_FOUND",
]
# The memo's next actions and two of an investigation's own. None is an outcome,
# and none runs by itself: a suggested screen is run by a person.
SuggestedCode = Literal[ActionCode, "SCREEN_RELATED_ENTITY", "REQUEST_DOCUMENT"]
DocumentKind = Literal[
    "SHAREHOLDER_REGISTER",
    "BENEFICIAL_OWNERSHIP_DECLARATION",
    "CORPORATE_REGISTRY_EXTRACT",
    "PROOF_OF_ADDRESS",
    "BOARD_RESOLUTION",
]
# Why a tool call was refused. The kind only, never a value.
ToolFault = Literal[
    "UNKNOWN_TOOL",
    "BAD_ARGUMENT",
    "UNSEEN_ID",
    "UNSEEN_RECORD",
    "NO_CONTEXT",
    "EGRESS_REFUSED",
    "BUDGET_SPENT",
]

FINDING_CODES: tuple[str, ...] = get_args(FindingCode)
SUGGESTED_CODES: tuple[str, ...] = (*get_args(ActionCode), "SCREEN_RELATED_ENTITY",
                                    "REQUEST_DOCUMENT")  # fmt: skip
DOCUMENT_KINDS: tuple[str, ...] = get_args(DocumentKind)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Finding(_Model):
    code: FindingCode
    # The entities it is about, as the analyst's view names them.
    subjects: list[NonEmptyStr]
    # Slotted: `{e:<id>}` for an entity, `{v:<call>.<pointer>}` for a value of a result.
    text: NonEmptyStr
    # The calls that show it (`c1`, `c2`, ...).
    cites: list[NonEmptyStr]


class SuggestedAction(_Model):
    code: SuggestedCode
    entity_id: NonEmptyStr | None = None
    document: DocumentKind | None = None


class ToolCallRecord(_Model):
    """One tool call of an investigation: what was asked and what the model was given."""

    call_id: NonEmptyStr
    tool: NonEmptyStr
    arguments: dict[str, Any]
    # The projected result, JSON values only. Empty for a refused call.
    items: list[dict[str, Any]] = []
    # How much a cap left out, counted, so the coverage can say so.
    truncated: int = 0
    fault: ToolFault | None = None
    # The earlier call this one repeated: the same result, and still a step.
    repeat_of: NonEmptyStr | None = None

    def result(self) -> dict[str, Any]:
        """What the model reads."""
        if self.fault:
            return {"call_id": self.call_id, "error": self.fault}
        return {"call_id": self.call_id, "items": self.items, "truncated": self.truncated}


class CappedCall(_Model):
    call_id: NonEmptyStr
    tool: NonEmptyStr
    left_out: int


class InvestigationCoverage(_Model):
    """How far an investigation looked, so "nothing found" can be told from "ran out"."""

    tools_called: list[str] = []
    # The ids the calls that answered asked about, as the analyst's view names them.
    entities_examined: list[str] = []
    # Calls a cap or a query's limit shortened, and by how much.
    capped: list[CappedCall] = []
    # Calls refused, by kind.
    refused: dict[str, int] = {}
    # Subjects of the first pass left out by its cap.
    subjects_not_examined: int = 0


class TurnRecord(_Model):
    """One model turn: enough to rebuild the next request and to check this one."""

    request_digest: NonEmptyStr
    # The blocks as they came, thinking included: the history is built from them.
    content: list[dict[str, Any]]
    stop_reason: NonEmptyStr
    # The model that answered: another than the one asked when a fallback ran.
    model: NonEmptyStr
    # The investigation's id of each tool call of the turn, in order.
    call_ids: list[str] = []
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    seconds: float = 0.0


class Investigation(_Model):
    """One question about one recommended decision. Advisory: no field of it reaches a
    decision. Made from the analyst's view (`egress`), so one record suits every role."""

    investigation_id: NonEmptyStr
    decision_id: NonEmptyStr
    snapshot_set_id: NonEmptyStr
    graph_digest: NonEmptyStr
    # None when the set has no current context layer.
    context_digest: str | None
    # As asked, or masked when it held a value the analyst may not see.
    question: NonEmptyStr
    egress: NonEmptyStr
    # What code found with no model; `findings` is what the model added.
    first_pass: list[Finding]
    findings: list[Finding] = []
    suggested_actions: list[SuggestedAction] = []
    coverage: InvestigationCoverage
    calls: list[ToolCallRecord]
    turns: list[TurnRecord] = []
    # None when no model was asked.
    model: str | None = None
    prompt_version: int
    prompt_sha256: NonEmptyStr
    budgets: dict[str, Any] = {}
    usage: dict[str, Any] = {}
    # False when a budget ran out or the model did not finish.
    complete: bool
    # Kinds of fault, never a value.
    faults: list[str] = []

    @property
    def event_id(self) -> str:
        return f"{RUN_PREFIX['INVESTIGATED']}{self.investigation_id}"
