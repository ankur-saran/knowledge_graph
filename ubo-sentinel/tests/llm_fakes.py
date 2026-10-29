"""Stand-ins for a model, for the Step 13 tests. No network, no key."""

from e2e import screen_gold

from ubo_sentinel.agents.supervisor import Supervisor
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.llm.client import LLMResponse, LLMUnavailable


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
