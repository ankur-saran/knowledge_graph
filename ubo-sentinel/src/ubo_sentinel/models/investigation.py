"""What an investigation holds (Step 14): typed findings, closed actions, each tool call."""

from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict

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
