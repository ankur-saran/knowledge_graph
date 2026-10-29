"""Intake query understanding (Step 13.5): variants from a model, candidates from the resolver."""

import csv

import pytest
from e2e import cli
from llm_fakes import FakeClient

from ubo_sentinel.agents import intake_assist
from ubo_sentinel.agents.intake_assist import (
    assist,
    check_interpretation,
    evaluate_assist,
    may_ask,
)
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.cli import screen_cmd
from ubo_sentinel.graph import queries
from ubo_sentinel.graph.store import GraphStore

MODEL = "claude-haiku-4-5"
HALCYON = "lei:FXS03TARGET000000000"


def variants(*names, jurisdiction=None, entity_type=None):
    return {
        "name_variants": list(names),
        "jurisdiction_hint": jurisdiction,
        "entity_type_hint": entity_type,
    }


@pytest.fixture
def store(fresh_con):
    con, (t0,) = fresh_con("t0")
    with GraphStore.open(t0, con) as opened:
        yield opened


@pytest.fixture
def llm_on(monkeypatch):
    for name in ("UBO_LLM", "UBO_LLM_INTAKE"):
        monkeypatch.setenv(name, "on")


def test_the_resolver_alone_misses_a_shortened_name(store):
    assert queries.resolve_entity(store, "Halcyon Ridge") == []


def test_a_variant_finds_what_the_query_did_not(store):
    client = FakeClient(variants("Halcyon Ridge Minerals Ltd", "Halcyon Ridge Limited"))
    run = assist(store, "Halcyon Ridge", [], client, MODEL, 500)
    assert run.status == "ACCEPTED" and run.faults == []
    assert [c.match.entity_id for c in run.candidates] == [HALCYON]
    assert run.candidates[0].variant == "Halcyon Ridge Minerals Ltd"
    # The model read the query and nothing else.
    assert client.requests[0].data == {"query": "Halcyon Ridge"}


def test_the_model_never_names_an_entity_only_the_resolver_does(store):
    client = FakeClient(variants("Halcyon Ridge Nonexistent Holdings"))
    run = assist(store, "Halcyon Ridge Holdings", [], client, MODEL, 500)
    assert run.status == "ACCEPTED"
    assert all(c.match.entity_id.startswith(("lei:", "fx_")) for c in run.candidates)


def test_candidates_the_resolver_already_gave_are_not_repeated(store):
    known = queries.resolve_entity(store, "Halcyon Ridge Mining")
    assert [m.entity_id for m in known][:1] == [HALCYON]
    run = assist(
        store,
        "Halcyon Ridge Mining",
        known,
        FakeClient(variants("Halcyon Ridge Minerals")),
        MODEL,
        500,
    )
    assert HALCYON not in [c.match.entity_id for c in run.candidates]


@pytest.mark.parametrize(
    ("output", "fault"),
    [
        (variants("Totally Different Shipping Co"), "a variant that is another name"),
        (variants(*["Halcyon Ridge Ltd"] * 9), "more variants than allowed"),
        (variants(""), "an empty or overlong variant"),
        (variants("Halcyon Ridge Ltd", jurisdiction="Narnia"), "a jurisdiction hint that is not"),
        ({"name_variants": "Halcyon"}, "variants that are not a list of names"),
    ],
)
def test_the_guardrail_refuses_a_variant_that_is_not_the_query(output, fault):
    assert any(f.startswith(fault) for f in check_interpretation(output, "Halcyon Ridge"))


def test_a_rejected_or_unavailable_answer_finds_nothing(store):
    rejected = assist(
        store, "Halcyon Ridge", [], FakeClient(variants("Other Name Entirely")), MODEL, 500
    )
    assert rejected.status == "REJECTED" and rejected.candidates == []
    unavailable = assist(store, "Halcyon Ridge", [], FakeClient(fail=True), MODEL, 500)
    assert unavailable.status == "UNAVAILABLE" and unavailable.candidates == []


def test_an_entity_type_hint_narrows_and_a_jurisdiction_hint_ranks(store):
    run = assist(
        store,
        "Halcyon Ridge",
        [],
        FakeClient(variants("Halcyon Ridge Minerals", entity_type="Person")),
        MODEL,
        500,
    )
    assert run.candidates == []
    run = assist(
        store,
        "Halcyon Ridge",
        [],
        FakeClient(variants("Halcyon Ridge Minerals", jurisdiction="cy")),
        MODEL,
        500,
    )
    assert [c.match.entity_id for c in run.candidates] == [HALCYON]


def test_no_model_is_asked_about_a_person_or_an_identifier(store, ontology):
    person = queries.resolve_entity(store, "Tobias Wrenfield")
    assert person and person[0].entity_type == "Person"
    assert not may_ask(person, ontology, identifiers=False)
    assert not may_ask([], ontology, identifiers=True)
    assert may_ask([], ontology, identifiers=False)


def test_a_run_is_recorded_once_and_folds_to_no_decision(store, ontology, fresh_db):
    log = AuditLog()
    run = assist(
        store, "Halcyon Ridge", [], FakeClient(variants("Halcyon Ridge Minerals")), MODEL, 500
    )
    from datetime import UTC, datetime

    now = datetime(2026, 10, 29, tzinfo=UTC)
    assert intake_assist.record(log, ontology, run, "a", "analyst", now)
    assert not intake_assist.record(log, ontology, run, "a", "analyst", now)
    (event,) = log.records()
    assert event["decision_id"] == f"intake:{run.run_id}"
    assert log.decisions() == [] and log.verify(ontology).intact


def test_recall_at_five_with_the_assist_is_never_lower(store):
    with open("eval/er_messy.csv", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    def answer(request):
        query = request.data["query"]
        return variants(f"{query} Minerals Ltd", f"{query} Consolidated plc")

    report = evaluate_assist(store, rows, FakeClient(answer), MODEL, 500)
    assert report.total == 12 and report.found_without == 9
    assert report.found_with >= 10 and report.recall_with >= report.recall_without
    offline = evaluate_assist(store, rows, FakeClient(fail=True), MODEL, 500)
    assert offline.found_with == offline.found_without and offline.not_assisted == 12


# --- the command -----------------------------------------------------------------------------


def _fake(monkeypatch, output):
    client = FakeClient(output)
    monkeypatch.setattr(screen_cmd, "client_for", lambda config: client)
    return client


def test_screen_lists_assisted_candidates_by_id_never_by_number(fresh_db, llm_on, monkeypatch):
    fresh_db("t0")
    _fake(monkeypatch, variants("Halcyon Ridge Minerals Ltd"))
    result = cli("screen", "Halcyon Ridge")
    assert result.exit_code == 0, result.output
    assert "ENTITY_NOT_FOUND" in result.output and "Intake assist (LLM-assisted" in result.output
    assert f"({HALCYON})" in result.output and "--target-id ID --assist-run" in result.output
    run_id = result.output.split("--assist-run ")[1].split()[0]

    screened = cli("screen", "--target-id", HALCYON, "--assist-run", run_id)
    assert screened.exit_code == 0, screened.output
    request = AuditLog().records()[-1]["payload"]["request"]
    assert request["assist_run_id"] == run_id and request["target_id"] == HALCYON


def test_an_assist_run_must_have_found_the_target(fresh_db, llm_on, monkeypatch):
    fresh_db("t0")
    _fake(monkeypatch, variants("Halcyon Ridge Minerals Ltd"))
    run_id = cli("screen", "Halcyon Ridge").output.split("--assist-run ")[1].split()[0]
    other = cli("screen", "--target-id", "lei:FXS01TARGET000000000", "--assist-run", run_id)
    assert other.exit_code == 2 and "did not find" in other.output
    assert cli("screen", "--target-id", HALCYON, "--assist-run", "nope").exit_code == 2
    assert cli("screen", "Halcyon Ridge", "--assist-run", run_id).exit_code == 2


def test_screen_with_the_assist_off_asks_no_model(fresh_db, monkeypatch):
    fresh_db("t0")
    monkeypatch.delenv("UBO_LLM", raising=False)
    client = _fake(monkeypatch, variants("Halcyon Ridge Minerals Ltd"))
    result = cli("screen", "Halcyon Ridge")
    assert result.exit_code == 0 and "Intake assist" not in result.output
    assert client.requests == []


def test_a_rejected_answer_shows_nothing(fresh_db, llm_on, monkeypatch):
    fresh_db("t0")
    _fake(monkeypatch, variants("Unrelated Name Group"))
    result = cli("screen", "Halcyon Ridge")
    assert "Intake assist: REJECTED" in result.output and "--assist-run" not in result.output
