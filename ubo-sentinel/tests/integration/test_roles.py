"""Who may run what, and what each role is shown."""

import pytest
from e2e import cli, person_pii, screen_gold

from ubo_sentinel.agents import supervisor as supervisor_module
from ubo_sentinel.agents.base import ScreenRequest
from ubo_sentinel.agents.supervisor import Supervisor
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.audit.review import review_decision
from ubo_sentinel.cli.app import app
from ubo_sentinel.cli.show import decision_text
from ubo_sentinel.models.canonical import canonical_json
from ubo_sentinel.models.memo import Memo

pytestmark = pytest.mark.integration

ROLES = ["analyst", "reviewer", "auditor", "engineer"]


def registered_commands():
    """Every command on the CLI, named as the permission matrix names it."""
    names = {c.name or c.callback.__name__.replace("_", "-") for c in app.registered_commands}
    for group in app.registered_groups:
        for command in group.typer_instance.registered_commands:
            names.add(f"{group.name}-{command.name or command.callback.__name__}")
    return names


def test_every_command_is_in_the_permission_matrix(ontology):
    in_matrix = set().union(*(spec.commands for spec in ontology.roles.values()))
    assert registered_commands() == in_matrix


# The cheapest call of each command that does what the command is for.
ARGS = {
    "ingest": ["ingest", "--source", "fixtures"],
    "normalize": ["normalize"],
    "build-graph": ["build-graph"],
    "eval": ["eval", "--suite", "er"],
    "screen": ["screen", "Acme Trading FZE", "--actor", "someone"],
    "review": ["review"],
    "audit-verify": ["audit", "verify"],
    "audit-replay": ["audit", "replay", "{decision_id}"],
}


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("command", sorted(ARGS))
def test_each_role_runs_exactly_its_commands(fresh_db, ontology, command, role):
    assert set(ARGS) == registered_commands()
    fresh_db("t0")
    decision = Supervisor(ontology, AuditLog()).run(ScreenRequest(query="Vostrek Maritime LLC"))
    args = [arg.format(decision_id=decision.decision.decision_id) for arg in ARGS[command]]

    result = cli(*args, "--role", role)
    permitted = command in ontology.roles[role].commands
    if permitted:
        assert result.exit_code == 0, result.output
    else:
        assert result.exit_code == 1 and "is not permitted" in result.output


# --- what each role is shown ---------------------------------------------------------------


@pytest.fixture
def decisions(fresh_db, con, ontology, gold):
    (t0,) = fresh_db("t0")
    supervisor = Supervisor(ontology, AuditLog())
    return {s.id: screen_gold(supervisor, s, t0, con).decision for s in gold}


def test_an_analyst_is_shown_no_person_in_any_scenario(decisions, ontology, entities):
    pii = person_pii(entities)
    shown_somewhere = set()
    for scenario_id, decision in decisions.items():
        for as_json in (False, True):
            text = decision_text(decision, "analyst", ontology, as_json=as_json)
            assert [value for value in pii if value in text] == [], scenario_id
        # The reviewer sees every person the memo describes.
        memo = Memo.model_validate(decision.memo_json)
        persons = [e.legal_name for e in memo.entities if e.entity_type == "Person"]
        shown = decision_text(decision, "reviewer", ontology)
        assert all(name in shown for name in persons), scenario_id
        shown_somewhere.update(persons)
    # The sweep looked at persons, not at an empty set.
    assert len(shown_somewhere) >= 4


def test_a_decision_is_the_same_whoever_screens(fresh_db, con, ontology, gold):
    (t0,) = fresh_db("t0")
    payloads = {}
    for role in ("analyst", "reviewer"):
        supervisor = Supervisor(ontology)
        payloads[role] = [
            canonical_json(screen_gold(supervisor, s, t0, con, role=role).decision.canonical_payload())
            for s in gold
        ]
    assert payloads["analyst"] == payloads["reviewer"]


def test_a_failure_shows_an_analyst_no_data(fresh_db, monkeypatch, entities):
    """An exception's text may quote the record it failed on: here, a person's name."""
    fresh_db("t0")

    def broken(self, state, ctx):
        raise ValueError("cannot read the owner Dmitri Volkanov (Дмитрий Волканов)")

    monkeypatch.setattr(supervisor_module.TraversalAgent, "run", broken)
    result = cli("screen", "Halcyon Ridge Minerals Ltd")
    assert result.exit_code == 1 and "(ValueError)" in result.output
    assert [value for value in person_pii(entities) if value in result.output] == []
    # The log, which only the governed roles read, keeps the whole error.
    assert "Dmitri Volkanov" in AuditLog().records()[0]["note"]


def test_who_may_write_and_who_may_review(fresh_db, ontology):
    fresh_db("t0")
    log = AuditLog()
    for role in ("auditor", "engineer"):
        with pytest.raises(PermissionError):
            Supervisor(ontology, log).run(ScreenRequest(query="Acme Trading FZE", role=role))
    decision = Supervisor(ontology, log).run(ScreenRequest(query="Acme Trading FZE")).decision
    for role in ("analyst", "auditor", "engineer"):
        with pytest.raises(PermissionError):
            review_decision(
                log,
                ontology,
                decision.decision_id,
                action="approve",
                actor="someone",
                role=role,
                now=lambda: decision.created_at,
            )
    assert [event["event_type"] for event in log.records()] == ["RECOMMENDED"]
