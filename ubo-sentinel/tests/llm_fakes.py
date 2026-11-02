"""Stand-ins for a model, for the Step 13 and 14 tests. No network, no key."""

from e2e import screen_gold

from ubo_sentinel.agents.supervisor import Supervisor
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.llm.client import LLMResponse, LLMUnavailable
from ubo_sentinel.llm.tools import ToolTurn


class FakeClient:
    """Answers every request with `output` (or a function of the request), and keeps them."""

    def __init__(self, output=None, fail=False):
        self.output = output
        self.fail = fail
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        if self.fail:
            raise LLMUnavailable("no model here")
        output = self.output(request) if callable(self.output) else self.output
        return LLMResponse(output=output, model=request.model, input_tokens=10, output_tokens=5)


def call(name, **arguments):
    """A `tool_use` block of a scripted turn; `ScriptedClient` gives it its id."""
    return {"type": "tool_use", "name": name, "input": arguments}


def say(text):
    return {"type": "text", "text": text}


class ScriptedClient:
    """A tool-using model that plays a script, one turn per step: a step is the turn's
    blocks, or a function of the request that gives them. Keeps every request; a
    turn with a tool call stops for it, any other ends. `fail_at` (1-based) is a turn
    the API fails."""

    def __init__(self, script, fail_at=None):
        self.script = list(script)
        self.fail_at = fail_at
        self.requests = []

    def converse(self, request):
        self.requests.append(request)
        turn = len(self.requests)
        if turn == self.fail_at:
            raise LLMUnavailable("no model here")
        if turn > len(self.script):
            raise AssertionError(f"the script has {len(self.script)} turns; turn {turn} asked")
        step = self.script[turn - 1]
        blocks = step(request) if callable(step) else step
        content = [
            {"id": f"toolu_{turn}_{i}", **block} if block["type"] == "tool_use" else block
            for i, block in enumerate(blocks)
        ]
        uses = any(block["type"] == "tool_use" for block in content)
        return ToolTurn(
            content=content,
            stop_reason="tool_use" if uses else "end_turn",
            model=request.model,
            input_tokens=100,
            output_tokens=20,
        )


def gold_decisions(fresh_con, ontology, gold, ids, log=None):
    """(connection, scenario id -> its decision), screened on t0 into `log` (default:
    UBO_VAR's). The connection is the test's: DuckDB lets a process open the file once."""
    con, (t0,) = fresh_con("t0")
    supervisor = Supervisor(ontology, log or AuditLog())
    by_id = {s.id: s for s in gold}
    return con, {i: screen_gold(supervisor, by_id[i], t0, con).decision for i in ids}


def good_prose(request):
    """Sentences that follow every rule: slots only, each claim that must be covered."""
    sentences = []
    for claim in request.data["claims"]:
        if claim["code"] == "COVERAGE":
            continue
        params = sorted(claim["params"])
        figure = f" at {{p:{claim['index']}.{params[0]}}}" if params else ""
        sentences.append(
            {
                "claim": claim["index"],
                "text": f"The memo states this about {{e:{claim['subject']}}}{figure};"
                " the rules give {rec}.",
            }
        )
    return {"sentences": sentences}
