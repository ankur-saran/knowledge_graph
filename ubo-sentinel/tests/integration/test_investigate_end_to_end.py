"""The investigator end to end (Step 14.8): `ubo investigate` per role, a model's findings
through recordings, replay, the eval, and the screens and reviews it leads to.

No network, no key: a scripted model makes the recordings a test replays.
"""

import json

import pytest
from conftest import INV
from e2e import cli, person_pii
from llm_fakes import ScriptedClient, call

from ubo_sentinel.agents.investigation_eval import run_investigation_suite
from ubo_sentinel.agents.investigator import investigate
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.llm.client import RecordedClient
from ubo_sentinel.llm.config import load_config
from ubo_sentinel.models import EntityRow, read_rows
from ubo_sentinel.models.investigation_scenario import load_investigation_scenarios
from ubo_sentinel.pipeline.db import connect

pytestmark = pytest.mark.integration

DESIGNATED = "lei:FXI03DESIG0000000000"
I05_A, I05_B = "lei:FXI05A00000000000000", "lei:FXI05B00000000000000"
QUESTION = "Who else is at this office?"
PII = [*person_pii(read_rows(INV / "entities.csv", EntityRow)), "Casimir Ondrejka"]
MODEL_ON = {"UBO_LLM": "on", "UBO_LLM_INVESTIGATE": "on", "UBO_LLM_MODE": "replay"}


@pytest.fixture
def screened(fresh_db):
    """A decision on the inv set, screened through the command: its id."""
    fresh_db("t0_inv")
    result = cli("screen", "--target-id", DESIGNATED, "--snapshot", "latest", "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.output[result.output.index("{") :])["decision_id"]


def investigation_id(output):
    return output.split("Recorded as investigation:")[1].split(".")[0]


def model_on(monkeypatch, recordings):
    for name, value in {**MODEL_ON, "UBO_LLM_RECORDINGS": str(recordings)}.items():
        monkeypatch.setenv(name, value)


def record_a_run(decision_id, recordings, ontology, script):
    """What a reviewed record session does: the model's turns kept under their digests."""
    decision = AuditLog().recommended(decision_id)
    recorder = RecordedClient(recordings, ScriptedClient(script))
    return investigate(decision, QUESTION, ontology, None, recorder, load_config().investigate)


# --- the command ---------------------------------------------------------------------------


def test_the_analyst_is_shown_a_pseudonym_and_the_reviewer_the_name(screened):
    analyst = cli("investigate", screened, QUESTION, "--no-llm")
    assert analyst.exit_code == 0, analyst.output
    assert "[SHARED_ADDRESS]" in analyst.output and "person:" in analyst.output
    assert [value for value in PII if value in analyst.output] == []
    assert "Recorded as investigation:" in analyst.output and "Audit head:" in analyst.output

    reviewer = cli("investigate", screened, QUESTION, "--no-llm", "--role", "reviewer",
                   "--actor", "rev")  # fmt: skip
    assert "Dmitri Volkanov" in reviewer.output
    assert "Already on record: the same run." in reviewer.output


def test_the_json_is_the_analysts_record_for_every_role(screened):
    for role in ("analyst", "reviewer"):
        result = cli("investigate", screened, QUESTION, "--no-llm", "--json", "--role", role)
        assert result.exit_code == 0, result.output
        document = json.loads(result.output)
        assert document["egress"] == "analyst_masked"
        assert [value for value in PII if value in result.output] == []


def test_with_the_model_off_the_first_pass_is_said_to_be_alone(screened):
    result = cli("investigate", screened, QUESTION)
    assert result.exit_code == 0
    assert "First pass only: the model is off" in result.output


def test_a_question_holding_a_name_is_not_kept(screened):
    result = cli("investigate", screened, "Is Casimir Ondrejka known?", "--json")
    assert json.loads(result.output)["question"] == "[PII MASKED]"
    assert "Casimir" not in result.output


@pytest.mark.parametrize(
    ("args", "code", "says"),
    [
        (["nope", QUESTION], 2, "No decision nope"),
        (["{id}", "  "], 2, "Give a question"),
        (["{id}", QUESTION, "--role", "auditor"], 1, "not permitted"),
    ],
)
def test_the_command_refuses_what_it_cannot_do(screened, args, code, says):
    result = cli("investigate", *[arg.format(id=screened) for arg in args])
    assert result.exit_code == code and says in result.output


# --- with a model, through recordings ----------------------------------------------------------


def submit_shared_place(request):
    """The model's finding, citing the call whose result it was last given."""
    call_id = json.loads(request.messages[-1]["content"][0]["content"])["call_id"]
    text = "{e:" + DESIGNATED + "} states the place in {v:" + call_id + "./items/0/country}."
    finding = {"code": "SHARED_ADDRESS", "subjects": [DESIGNATED], "text": text,
               "cites": [call_id]}  # fmt: skip
    action = {"code": "SCREEN_RELATED_ENTITY", "entity_id": "fx_list:i06-owner", "document": None}
    return [call("submit_findings", findings=[finding], suggested_actions=[action])]


def test_a_recorded_model_run_replays_through_the_command(screened, ontology, monkeypatch,
                                                          tmp_path):  # fmt: skip
    script = [[call("get_entities_sharing_address", entity_id=DESIGNATED)], submit_shared_place]
    made = record_a_run(screened, tmp_path, ontology, script)
    assert made.findings and made.complete
    model_on(monkeypatch, tmp_path)

    result = cli("investigate", screened, QUESTION)
    assert result.exit_code == 0, result.output
    assert "LLM-assisted, advisory" in result.output
    assert "states the place in KZ" in result.output
    assert f"--investigation-id {made.investigation_id}" in result.output
    assert investigation_id(result.output) == made.investigation_id
    assert [value for value in PII if value in result.output] == []

    replayed = cli("audit", "replay-investigation", made.investigation_id, "--deep")
    assert replayed.exit_code == 0, replayed.output
    assert "Replay identical" in replayed.output


def test_without_a_recording_the_model_is_unavailable_and_the_first_pass_is_kept(
    screened, monkeypatch, tmp_path
):
    model_on(monkeypatch, tmp_path)
    result = cli("investigate", screened, QUESTION)
    assert result.exit_code == 1
    assert "Faults: model unavailable" in result.output and "[SHARED_ADDRESS]" in result.output
    assert "Recorded as investigation:" in result.output


def test_live_mode_without_the_sdk_says_so(screened, monkeypatch):
    monkeypatch.setenv("UBO_LLM", "on")
    monkeypatch.setenv("UBO_LLM_INVESTIGATE", "on")
    monkeypatch.setenv("UBO_LLM_MODE", "live")
    import ubo_sentinel.cli.investigate_cmd as command

    def no_sdk(config):
        raise command.LLMUnavailable("The Anthropic SDK is not installed.")

    monkeypatch.setattr(command, "client_for", no_sdk)
    result = cli("investigate", screened, QUESTION)
    assert result.exit_code == 1 and "No model" in result.output


def test_the_eval_scores_a_recorded_scenario_and_says_the_rest_were_not_run(
    fresh_db, ontology, monkeypatch, tmp_path
):
    _, inv_set = fresh_db("t0_inv")
    scenarios = [s for s in load_investigation_scenarios(INV.parent.parent / "eval"
                 / "investigation_scenarios.yaml") if s.id == "INV-05"]  # fmt: skip
    path = call("find_paths_between", source_id=I05_A, target_id=I05_B, max_depth=3)

    def submit(request):
        call_id = json.loads(request.messages[-1]["content"][0]["content"])["call_id"]
        text = "{e:" + I05_A + "} and {e:" + I05_B + "} are joined by holdings."
        finding = {"code": "PATH_BETWEEN", "subjects": [I05_A, I05_B], "text": text,
                   "cites": [call_id]}  # fmt: skip
        return [call("submit_findings", findings=[finding], suggested_actions=[])]

    con = connect()
    try:
        recorder = RecordedClient(tmp_path, ScriptedClient([[path], submit]))
        run_investigation_suite(con, inv_set, ontology, scenarios, AuditLog(tmp_path / "log"),
                                recorder, load_config().investigate)  # fmt: skip
    finally:
        con.close()
    model_on(monkeypatch, tmp_path)
    result = cli("eval", "--suite", "investigator", "--snapshot", "latest")
    assert "INV-05  PASS" in result.output, result.output
    assert "Lift over the first pass  100.0%" in result.output
    assert "Not run (no recording or no model): INV-01" in result.output


# --- what an investigation leads to -------------------------------------------------------------


def test_a_suggested_screen_names_its_investigation_beside_the_request(screened):
    made = investigation_id(cli("investigate", screened, QUESTION, "--no-llm").output)
    result = cli("screen", "--target-id", "fx_list:i06-owner", "--snapshot", "latest",
                 "--investigation-id", made)  # fmt: skip
    assert result.exit_code == 0, result.output
    event = AuditLog().records()[-1]
    assert event["payload"]["request"]["investigation_id"] == made
    unknown = cli("screen", "--target-id", "fx_list:i06-owner", "--snapshot", "latest",
                  "--investigation-id", "nope")  # fmt: skip
    assert unknown.exit_code == 2 and "No investigation nope" in unknown.output


def test_the_same_screen_with_and_without_an_investigation_is_one_decision(screened):
    made = investigation_id(cli("investigate", screened, QUESTION, "--no-llm").output)
    args = ["screen", "--target-id", "fx_list:i06-owner", "--snapshot", "latest", "--json"]
    first = cli(*args)
    second = cli(*args, "--investigation-id", made)
    ids = [json.loads(r.output[r.output.index("{") :])["decision_id"] for r in (first, second)]
    assert ids[0] == ids[1], "the investigation is beside the request, not in the decision"


def test_a_review_records_which_investigations_were_read(screened):
    made = investigation_id(cli("investigate", screened, QUESTION, "--no-llm").output)
    base = ["review", screened, "--role", "reviewer", "--actor", "rev", "--action", "approve"]
    refused = cli(*base, "--considered", "nope")
    assert refused.exit_code == 2 and "No investigation nope" in refused.output
    result = cli(*base, "--considered", made)
    assert result.exit_code == 0, result.output
    review = AuditLog().records()[-1]
    assert review["event_type"] == "APPROVED"
    assert review["payload"]["considered_investigations"] == [made]


def test_a_review_that_names_none_is_written_as_before(screened):
    cli("review", screened, "--role", "reviewer", "--actor", "rev", "--action", "approve")
    assert "considered_investigations" not in AuditLog().records()[-1]["payload"]
