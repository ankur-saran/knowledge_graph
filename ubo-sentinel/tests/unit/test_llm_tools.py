"""The LLM layer for tools (Step 14.2): a turn request, the API call, recordings, the
scripted fake. No network, no key."""

from types import SimpleNamespace

import pytest
from anthropic.types.beta import BetaMessage
from llm_fakes import ScriptedClient, call, say
from pydantic import ValidationError

from ubo_sentinel.llm.client import (
    FALLBACK_BETA,
    TASK_BUDGET_BETA,
    AnthropicClient,
    LLMUnavailable,
    RecordedClient,
)
from ubo_sentinel.llm.tools import (
    ToolResult,
    ToolSpec,
    ToolTurnRequest,
    results_message,
    server_fallback,
    user_message,
)

ENTITY = ToolSpec(
    name="get_entity",
    description="One entity by id.",
    input_schema={
        "type": "object",
        "properties": {"entity_id": {"type": "string"}},
        "required": ["entity_id"],
        "additionalProperties": False,
    },
)
OWNERS = ENTITY.model_copy(update={"name": "get_direct_owners", "description": "Its owners."})

THINKING = {"type": "thinking", "thinking": "Who owns it?", "signature": "c2lnbmVk"}
USE = {"type": "tool_use", "id": "toolu_01", "name": "get_entity", "input": {"entity_id": "e1"}}


def _request(**update):
    base = dict(
        feature="investigate",
        model="claude-opus-5-5",
        system="frozen prompt",
        tools=(ENTITY, OWNERS),
        messages=(user_message("question"),),
    )
    return ToolTurnRequest(**(base | update))


class FakeSdk:
    """The surface of `anthropic.Anthropic` a turn uses: `beta.messages.create`, answered
    with the SDK's own message type."""

    def __init__(self, content=(THINKING, USE), stop_reason="tool_use", error=None):
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))
        self.content, self.stop_reason, self.error = list(content), stop_reason, error

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return BetaMessage.model_validate(
            {
                "id": "msg_01",
                "type": "message",
                "role": "assistant",
                "model": kwargs["model"],
                "stop_reason": self.stop_reason,
                "stop_sequence": None,
                "content": self.content,
                "usage": {
                    "input_tokens": 900,
                    "output_tokens": 40,
                    "cache_read_input_tokens": 800,
                    "cache_creation_input_tokens": None,
                },
            }
        )


# --- the request ------------------------------------------------------------------------


def test_a_turn_request_is_data_with_a_digest_and_its_tools_in_name_order():
    request = _request()
    assert [tool.name for tool in request.tools] == ["get_direct_owners", "get_entity"]
    assert request.digest() == _request(tools=(OWNERS, ENTITY)).digest()
    assert request.digest() != _request(messages=(user_message("another"),)).digest()


@pytest.mark.parametrize(
    "update",
    [
        {"tools": (ENTITY, ENTITY)},
        {"task_budget": 19_999},
    ],
    ids=["two tools of one name", "a budget under the API's minimum"],
)
def test_a_request_the_api_would_refuse_is_not_made(update):
    with pytest.raises(ValidationError):
        _request(**update)


def test_a_tool_is_strict_and_its_input_a_closed_object():
    assert ENTITY.api()["strict"] is True
    with pytest.raises(ValidationError, match="additionalProperties"):
        ToolSpec(name="t", description="d", input_schema={"type": "object", "properties": {}})


def test_results_answer_every_call_as_canonical_json_and_a_note_follows_them():
    message = results_message(
        [
            ToolResult(tool_use_id="toolu_01", content={"b": 1, "a": [2]}),
            ToolResult(tool_use_id="toolu_02", content={"error": "UNSEEN_ID"}, is_error=True),
        ],
        note="Budget reached: submit your findings now.",
    )
    first, second, note = message["content"]
    assert message["role"] == "user"
    assert first == {
        "type": "tool_result",
        "tool_use_id": "toolu_01",
        "content": '{"a":[2],"b":1}',
        "is_error": False,
    }
    assert second["is_error"] is True
    assert note == {"type": "text", "text": "Budget reached: submit your findings now."}
    assert results_message([ToolResult(tool_use_id="t", content=[])])["content"][-1]["type"] == (
        "tool_result"
    )


def test_haiku_has_no_server_side_fallback():
    assert server_fallback("claude-opus-5-5") and not server_fallback("claude-haiku-5-5")


# --- the API call -----------------------------------------------------------------------


def test_a_turn_asks_for_auto_tool_choice_strict_tools_adaptive_thinking_and_the_betas():
    sdk = FakeSdk()
    turn = AnthropicClient(10, sdk).converse(_request(task_budget=200_000))
    sent = sdk.calls[0]
    assert sent["tool_choice"] == {"type": "auto"}
    assert [tool["name"] for tool in sent["tools"]] == ["get_direct_owners", "get_entity"]
    assert all(tool["strict"] for tool in sent["tools"])
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"] == {
        "effort": "high",
        "task_budget": {"type": "tokens", "total": 200_000},
    }
    assert sent["cache_control"] == {"type": "ephemeral"}
    assert sent["fallbacks"] == "default"
    assert sent["betas"] == [TASK_BUDGET_BETA, FALLBACK_BETA]
    assert sent["system"] == [{"type": "text", "text": "frozen prompt"}]

    assert turn.stop_reason == "tool_use"
    assert [(c.id, c.name, c.input) for c in turn.calls] == [
        ("toolu_01", "get_entity", {"entity_id": "e1"})
    ]
    assert (turn.input_tokens, turn.output_tokens, turn.cache_read_tokens) == (900, 40, 800)


def test_without_a_budget_or_a_fallback_no_beta_is_asked():
    sdk = FakeSdk()
    AnthropicClient(10, sdk).converse(_request(model="claude-haiku-5-5", fallbacks=False))
    sent = sdk.calls[0]
    assert "fallbacks" not in sent and sent["betas"] == []
    assert sent["output_config"] == {"effort": "high"}


def test_the_turn_goes_back_into_the_history_unchanged_thinking_and_all():
    turn = AnthropicClient(10, FakeSdk()).converse(_request())
    assert turn.message() == {"role": "assistant", "content": [THINKING, USE]}


def test_a_paused_turn_is_returned_for_the_caller_to_send_back():
    sdk = FakeSdk(content=[say("Still looking.")], stop_reason="pause_turn")
    turn = AnthropicClient(10, sdk).converse(_request())
    assert turn.stop_reason == "pause_turn" and turn.calls == [] and turn.text == "Still looking."


@pytest.mark.parametrize(
    "sdk",
    [
        FakeSdk(content=[], stop_reason="refusal"),
        FakeSdk(stop_reason="max_tokens"),
        FakeSdk(content=[], stop_reason="model_context_window_exceeded"),
        FakeSdk(error=TimeoutError("slow")),
    ],
    ids=["refusal", "cut off", "context full", "api error"],
)
def test_a_turn_that_cannot_be_acted_on_is_unavailable(sdk):
    with pytest.raises(LLMUnavailable):
        AnthropicClient(10, sdk).converse(_request())


# --- recordings -------------------------------------------------------------------------


def test_each_turn_is_recorded_once_and_replayed_without_the_model(tmp_path):
    sdk = FakeSdk()
    recorder = RecordedClient(tmp_path, AnthropicClient(10, sdk))
    first = recorder.converse(_request())
    replayed = RecordedClient(tmp_path).converse(_request())
    assert replayed == first and len(sdk.calls) == 1
    assert recorder.path_for(_request()).name.startswith("investigate-")


def test_a_turn_after_a_changed_tool_result_has_no_recording(tmp_path):
    sdk = FakeSdk()
    turn = AnthropicClient(10, sdk).converse(_request())

    def after(result):
        history = (
            user_message("question"),
            turn.message(),
            results_message([ToolResult(tool_use_id="toolu_01", content=result)]),
        )
        return _request(messages=history)

    RecordedClient(tmp_path, AnthropicClient(10, sdk)).converse(after({"name": "Orvane"}))
    RecordedClient(tmp_path).converse(after({"name": "Orvane"}))
    with pytest.raises(LLMUnavailable, match="No recorded response"):
        RecordedClient(tmp_path).converse(after({"name": "Orvane Ltd"}))


# --- the scripted fake ------------------------------------------------------------------


def test_the_scripted_fake_plays_a_loop_its_caller_owns():
    client = ScriptedClient(
        [
            [say("Looking."), call("get_entity", entity_id="e1")],
            lambda request: [say(f"{len(request.messages)} messages read.")],
        ]
    )
    history = [user_message("question")]
    turn = client.converse(_request(messages=tuple(history)))
    assert turn.stop_reason == "tool_use" and turn.calls[0].id == "toolu_1_1"
    history += [
        turn.message(),
        results_message([ToolResult(tool_use_id=turn.calls[0].id, content={"id": "e1"})]),
    ]
    last = client.converse(_request(messages=tuple(history)))
    assert (last.stop_reason, last.text) == ("end_turn", "3 messages read.")
    with pytest.raises(AssertionError, match="2 turns"):
        client.converse(_request())
    with pytest.raises(LLMUnavailable):
        ScriptedClient([[say("x")]], fail_at=1).converse(_request())
