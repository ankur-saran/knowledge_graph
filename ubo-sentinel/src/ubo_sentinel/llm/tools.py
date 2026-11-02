"""One turn of a conversation in which the model may call tools (Step 14).

The loop belongs to its caller, not to the SDK's tool runner: every tool call is
masked, counted and recorded by the investigator. A turn request is data (the
system prompt, the tools, the whole history), so it has a digest and a recording
is keyed by it, as `LLMRequest`'s is: replay is per turn, and a turn whose
history differs (a tool result that changed) has no recording.

The history is append-only. An assistant turn goes back exactly as it came,
thinking blocks and all: Claude Opus 5.5 checks that its earlier thinking is
unchanged, and an edited prefix also misses the prompt cache.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ubo_sentinel.models.canonical import canonical_json, sha256_hex

# A task budget below this is refused by the API.
MIN_TASK_BUDGET = 20_000
# One turn's output: a few tool calls and their thinking, or the findings. Under
# the ~21 000 tokens above which the SDK asks for streaming.
TURN_MAX_TOKENS = 16_000

Effort = Literal["low", "medium", "high", "xhigh", "max"]
# A turn the caller acts on: tool calls to run, an answer, or a paused turn to
# send back as it is (the server continues it). Any other stop is `LLMUnavailable`.
StopReason = Literal["tool_use", "end_turn", "pause_turn"]


class ToolSpec(BaseModel):
    """A tool as the model sees it. Strict: the API holds every input to the schema."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str
    input_schema: dict[str, Any]

    @field_validator("input_schema")
    @classmethod
    def _closed_object(cls, schema: dict[str, Any]) -> dict[str, Any]:
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise ValueError("a strict tool's input is an object with additionalProperties false")
        return schema

    def api(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "strict": True,
        }


class ToolTurnRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    feature: str
    model: str
    # Frozen and versioned by the caller; with the tools, the cached prefix.
    system: str
    # Sorted by name, so the same tools are the same bytes.
    tools: tuple[ToolSpec, ...]
    # The whole history, oldest first, in the API's shape.
    messages: tuple[dict[str, Any], ...]
    max_tokens: int = TURN_MAX_TOKENS
    effort: Effort = "high"
    # Tokens the whole loop may spend, told to the model so it paces itself
    # (advisory: the caller enforces its own caps). None sends no budget.
    task_budget: int | None = None
    # Retry a declined turn on the model Anthropic names for the refusal's kind.
    # Claude Haiku has no server-side fallback.
    fallbacks: bool = True

    @field_validator("tools")
    @classmethod
    def _sorted(cls, tools: tuple[ToolSpec, ...]) -> tuple[ToolSpec, ...]:
        names = [tool.name for tool in tools]
        if len(set(names)) != len(names):
            raise ValueError("two tools share a name")
        return tuple(sorted(tools, key=lambda tool: tool.name))

    @field_validator("task_budget")
    @classmethod
    def _budget(cls, budget: int | None) -> int | None:
        if budget is not None and budget < MIN_TASK_BUDGET:
            raise ValueError(f"a task budget is at least {MIN_TASK_BUDGET} tokens")
        return budget

    def digest(self) -> str:
        return sha256_hex(self.model_dump(mode="json"))


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # The API's `tool_use` id; a `tool_result` answers it.
    id: str
    name: str
    input: dict[str, Any]


class ToolTurn(BaseModel):
    """The model's turn: its content blocks as JSON, to go back into the history."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: list[dict[str, Any]]
    stop_reason: StopReason
    # The model that answered: another than the one asked when a fallback ran.
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    seconds: float = 0.0

    @property
    def calls(self) -> list[ToolCall]:
        return [
            ToolCall(id=block["id"], name=block["name"], input=block["input"])
            for block in self.content
            if block.get("type") == "tool_use"
        ]

    @property
    def text(self) -> str:
        return "\n".join(b["text"] for b in self.content if b.get("type") == "text")

    def message(self) -> dict[str, Any]:
        """The turn as the next request's history holds it, unchanged."""
        return {"role": "assistant", "content": self.content}


class ToolResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_use_id: str
    # Sent as canonical JSON text: the same result is the same bytes.
    content: Any
    is_error: bool = False

    def block(self) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": self.tool_use_id,
            "content": canonical_json(self.content).decode("utf-8"),
            "is_error": self.is_error,
        }


def user_message(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def results_message(results: list[ToolResult], note: str | None = None) -> dict[str, Any]:
    """One user turn answering every call of the turn before it, in its order. A `note`
    (the caller's, never data) follows the results as text."""
    content = [result.block() for result in results]
    if note:
        content.append({"type": "text", "text": note})
    return {"role": "user", "content": content}


def server_fallback(model: str) -> bool:
    """Whether the model has a server-side fallback (Claude Haiku has none)."""
    return not model.startswith("claude-haiku")
