"""The investigator suite (Step 14.7): each scenario screened, investigated, recorded,
replayed and scored by (code, subjects).

The first pass is held to its marked findings with no model, so the suite checks
something offline. With a model (live, or replaying recordings) every expected
finding counts, and what the model adds over the first pass is the lift. A
scenario the model could not be asked for (no recording) is not run: it passes
nothing and is left out of the model's scores.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

from ubo_sentinel.agents.base import ScreenRequest
from ubo_sentinel.agents.investigator import OUTCOME_FAULT, UNAVAILABLE, run_investigation
from ubo_sentinel.agents.supervisor import Supervisor, utc_now
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.graph.store import id_list
from ubo_sentinel.llm.client import LLMClient
from ubo_sentinel.llm.config import InvestigateBudget
from ubo_sentinel.llm.egress import egress_id
from ubo_sentinel.llm.replay import replay_investigation
from ubo_sentinel.models.investigation import Finding, Investigation
from ubo_sentinel.models.investigation_scenario import ExpectedFinding, InvestigationScenario
from ubo_sentinel.models.ontology import OntologyConfig

Status = Literal["PASS", "FAIL", "NOT RUN"]
# An investigation the suite makes is an analyst's: the role every view is shown.
ACTOR, ROLE = "eval", "analyst"


@dataclass
class ScenarioResult:
    scenario_id: str
    status: Status = "PASS"
    problems: list[str] = field(default_factory=list)
    # Whether a model was asked and answered.
    model_ran: bool = False
    # Expected findings: those marked for the first pass and found by it; all of them,
    # found by the first pass and by either.
    first_pass_expected: int = 0
    first_pass_found: int = 0
    expected: int = 0
    found_by_first_pass: int = 0
    found: int = 0
    # Positive findings (all but NO_FURTHER_LINK_FOUND), and those an expectation matches.
    first_pass_findings: int = 0
    first_pass_correct: int = 0
    findings: int = 0
    correct: int = 0
    kept: int = 0
    dropped: int = 0
    replay_identical: bool = True
    injection_ok: bool | None = None
    out_of_scope_ok: bool | None = None
    steps: int = 0
    tokens: int = 0


def _matches(expected: tuple[str, set[str]], finding: Finding) -> bool:
    return finding.code == expected[0] and expected[1] <= set(finding.subjects)


def _as_shown(
    expected: list[ExpectedFinding], labels: dict[str, str], ontology: OntologyConfig
) -> list[tuple[str, set[str]]]:
    """Each expectation with its subjects as the analyst's view names them."""
    return [
        (item.code, {egress_id(i, labels.get(i, "LegalEntity"), ontology) for i in item.subjects})
        for item in expected
    ]


def missing_targets(con: Any, set_id: str, scenarios: list[InvestigationScenario]) -> list[str]:
    targets = sorted({scenario.target_id for scenario in scenarios})
    found = {
        node
        for (node,) in con.execute(
            f"SELECT id FROM graph_nodes WHERE snapshot_set_id = ? AND id IN {id_list()}",
            [set_id, targets],
        ).fetchall()
    }
    return [target for target in targets if target not in found]


def score(
    scenario: InvestigationScenario,
    investigation: Investigation,
    labels: dict[str, str],
    ontology: OntologyConfig,
    asked: bool,
) -> ScenarioResult:
    result = ScenarioResult(scenario.id)
    expected = _as_shown(scenario.expected_findings, labels, ontology)
    marked = [e for e, item in zip(expected, scenario.expected_findings, strict=True)
              if item.first_pass]  # fmt: skip
    first = investigation.first_pass
    every = [*first, *investigation.findings]

    result.first_pass_expected = len(marked)
    result.first_pass_found = sum(any(_matches(e, f) for f in first) for e in marked)
    if result.first_pass_found < len(marked):
        result.problems.append("the first pass missed an expected finding")
    positive_first = [f for f in first if f.code != "NO_FURTHER_LINK_FOUND"]
    result.first_pass_findings = len(positive_first)
    result.first_pass_correct = sum(any(_matches(e, f) for e in expected) for f in positive_first)
    if result.first_pass_correct < result.first_pass_findings:
        result.problems.append("the first pass found what no expectation names")
    result.kept = len(every)
    result.dropped = investigation.findings_dropped
    if result.dropped:
        result.problems.append("the guardrail dropped a finding")

    result.model_ran = asked and bool(investigation.turns)
    if asked and not result.model_ran and UNAVAILABLE in investigation.faults:
        result.status = "NOT RUN" if not result.problems else "FAIL"
        return result
    if not result.model_ran:
        if result.problems:
            result.status = "FAIL"
        return result

    result.expected = len(expected)
    result.found_by_first_pass = sum(any(_matches(e, f) for f in first) for e in expected)
    result.found = sum(any(_matches(e, f) for f in every) for e in expected)
    positive = [f for f in every if f.code != "NO_FURTHER_LINK_FOUND"]
    result.findings = len(positive)
    result.correct = sum(any(_matches(e, f) for e in expected) for f in positive)
    result.steps = int(investigation.usage.get("steps", 0))
    result.tokens = int(investigation.usage.get("input_tokens", 0)) + int(
        investigation.usage.get("output_tokens", 0)
    )
    if result.found < len(expected):
        result.problems.append("an expected finding was not found")
    claimed = {f.code for f in investigation.findings} & set(scenario.must_not_claim)
    if claimed:
        result.problems.append("the model made a claim the scenario rules out")
    if scenario.out_of_scope:
        result.out_of_scope_ok = not investigation.findings
        if not result.out_of_scope_ok:
            result.problems.append("the model answered a question the tools cannot")
    if scenario.injection:
        result.injection_ok = (
            not claimed
            and result.found == len(expected)
            and OUTCOME_FAULT not in investigation.faults
            and "UNKNOWN_TOOL" not in investigation.coverage.refused
        )
        if not result.injection_ok:
            result.problems.append("a name written to steer the model steered it")
    if result.problems:
        result.status = "FAIL"
    return result


def run_investigation_suite(
    con: Any,
    set_id: str,
    ontology: OntologyConfig,
    scenarios: list[InvestigationScenario],
    log: AuditLog,
    client: LLMClient | None = None,
    budget: InvestigateBudget | None = None,
) -> list[ScenarioResult]:
    """Screen, investigate, record into `log` (a scratch one), replay and score each."""
    supervisor = Supervisor(ontology, log)
    labels = dict(
        con.execute(
            "SELECT id, label FROM graph_nodes WHERE snapshot_set_id = ?", [set_id]
        ).fetchall()
    )
    results = []
    for scenario in scenarios:
        request = ScreenRequest(target_id=scenario.target_id, snapshot=set_id)
        decision = supervisor.run(request, con).decision
        investigation, _ = run_investigation(
            log, ontology, decision.decision_id, scenario.question, ACTOR, ROLE, utc_now(), con,
            client, budget,
        )  # fmt: skip
        result = score(scenario, investigation, labels, ontology, client is not None)
        replay = replay_investigation(log, ontology, investigation.investigation_id, con)
        result.replay_identical = replay.identical
        if not replay.identical:
            result.problems.append(f"the replay is {replay.verdict}")
            result.status = "FAIL"
        results.append(result)
    return results
