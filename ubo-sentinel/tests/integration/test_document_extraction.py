"""Step 15 through the commands: a document read, a holding confirmed by another person,
published, and changing a screen only through the deterministic pipeline; then revoked."""

import json
import re
from pathlib import Path

import pytest
from e2e import cli, decision_id_of

pytestmark = pytest.mark.integration

ACME = "lei:FXS01TARGET000000000"
VOSTREK = "lei:FXS02TARGET000000000"
NARRATIVE = "eval/extraction/doc-02-narrative.txt"
REGISTER = "eval/extraction/doc-01-register.txt"
GOLDEN = json.loads(Path("eval/golden_decisions.json").read_text(encoding="utf-8"))


def proposal_ids(output):
    return re.findall(r"^\[[A-Z_]+\] ([0-9a-f]{16})", output, flags=re.M)


def ok(*args):
    result = cli(*args)
    assert result.exit_code == 0, result.output
    return result.output


def rebuild():
    ok("normalize")
    ok("build-graph")


def screen(snapshot):
    output = ok("screen", "Acme Trading FZE", "--snapshot", snapshot, "--actor", "ana")
    return decision_id_of(output), output


def test_a_confirmed_holding_changes_a_screen_only_through_the_pipeline(fresh_db):
    fresh_db("t0")
    before, shown = screen("fixtures")
    # The decision on t0 is the golden one: nothing in Step 15 moves an id.
    assert before == GOLDEN["decisions"]["SCEN-01"]["decision_id"] and "CLEAR" in shown

    extracted = ok("extract", NARRATIVE, "--subject", ACME, "--actor", "ana")
    assert "First pass only" in extracted and "Recorded as extraction:" in extracted
    (proposal,) = proposal_ids(extracted)
    assert "Already on record" in ok("extract", NARRATIVE, "--subject", ACME, "--actor", "ana")
    extraction_id = re.search(r"Extraction ([0-9a-f]{16})", extracted).group(1)

    # Nothing reaches the graph before a ruling and a publish.
    assert "Nothing to publish" in cli("ingest", "--source", "documents").output
    assert proposal in ok("extract-review", "--role", "reviewer")
    same = cli("extract-review", proposal, "--role", "reviewer", "--actor", "ana",
               "--action", "accept", "--owner", VOSTREK, "--as-of", "2026-09-30")  # fmt: skip
    assert same.exit_code == 1 and "extracted it" in same.output
    ok("extract-review", proposal, "--role", "reviewer", "--actor", "rita",
       "--action", "accept", "--owner", VOSTREK, "--as-of", "2026-09-30")  # fmt: skip
    assert "No proposal waits" in ok("extract-review", "--role", "reviewer")

    published = ok("ingest", "--source", "documents")
    assert "bronze_relationships" in published and "1 rows" in published
    rebuild()
    after, shown = screen("latest")
    assert after != before and "ESCALATE" in shown and "DERIVED_50PCT" in shown
    assert "document:" in shown
    # The decision on t0 is untouched, and the monitor queues it with its cause.
    assert screen("fixtures")[0] == before
    queued = ok("monitor", "--dry-run", "--curr", "latest", "--prev", "fixtures")
    assert before in queued and "OWNERSHIP" in queued

    assert "Replay identical" in ok("audit", "replay-extraction", extraction_id)
    assert "Chain intact" in ok("audit", "verify")

    # Revoked, then published: the holding leaves, and the screen is as it was.
    ok("extract-review", proposal, "--role", "reviewer", "--actor", "rita",
       "--action", "revoke", "--reason", "the statement was withdrawn")  # fmt: skip
    ok("ingest", "--source", "documents")
    rebuild()
    again, shown = screen("latest")
    assert again not in (before, after) and "CLEAR" in shown


def test_an_analyst_is_shown_no_person_a_document_names(fresh_db, entities):
    fresh_db("t0")
    shown = ok("extract", REGISTER, "--subject", ACME, "--actor", "ana")
    assert "Tobias Wrenfield" not in shown and "withheld" in shown
    assert "Tobias Wrenfield" in ok("extract-review", "--role", "reviewer")
    as_json = ok("extract", REGISTER, "--subject", ACME, "--actor", "ana", "--json")
    # The record is made once and kept unmasked, as a memo is.
    assert json.loads(as_json)["proposals"]


def test_the_commands_refuse_what_they_cannot_do(fresh_db, tmp_path):
    fresh_db("t0")
    scan = tmp_path / "scan.pdf"
    scan.write_bytes(b"%PDF-1.4 nothing")
    assert cli("extract", scan, "--subject", ACME).exit_code == 2
    assert cli("extract", NARRATIVE, "--subject", "lei:NOSUCH").exit_code == 2
    assert (
        cli("extract", NARRATIVE, "--subject", ACME, "--investigation-id", "x" * 16).exit_code == 2
    )
    assert cli("extract-review").exit_code == 2
    assert cli("extract-review", "nosuch", "--role", "reviewer").exit_code == 2
    assert cli("audit", "replay-extraction", "nosuch").exit_code == 2
    extracted = ok("extract", NARRATIVE, "--subject", ACME, "--actor", "ana")
    (proposal,) = proposal_ids(extracted)
    assert "page 1" in ok("extract-review", proposal, "--role", "reviewer")
    missing = cli("extract-review", proposal, "--role", "reviewer", "--action", "accept")
    assert missing.exit_code == 2
    no_date = cli("extract-review", proposal, "--role", "reviewer", "--actor", "rita",
                  "--action", "accept", "--owner", VOSTREK)  # fmt: skip
    assert no_date.exit_code == 1 and "effective date" in no_date.output


def test_a_model_is_off_unless_asked_and_given_consent(fresh_db, monkeypatch):
    fresh_db("t0")
    monkeypatch.setenv("UBO_LLM", "on")
    monkeypatch.setenv("UBO_LLM_EXTRACT", "on")
    monkeypatch.setenv("UBO_LLM_MODE", "replay")
    assert "only with --allow-egress" in ok("extract", NARRATIVE, "--subject", ACME)
    # With consent and no recording, the model answers nothing: the first pass is kept.
    asked = cli("extract", NARRATIVE, "--subject", ACME, "--allow-egress")
    assert asked.exit_code == 1 and "MODEL_UNAVAILABLE" in asked.output
    assert proposal_ids(asked.output)
    assert "--no-llm" in ok("extract", NARRATIVE, "--subject", ACME, "--no-llm")


def test_the_extraction_suite_holds_the_first_pass_offline(fresh_db):
    fresh_db("t0")
    output = ok("eval", "--suite", "extraction", "--snapshot", "fixtures")
    assert "First-pass recall       100.0%" in output and "Model scores: not run" in output
    assert cli("eval", "--suite", "extraction", "--snapshot", "fixtures", "--json").exit_code == 0
