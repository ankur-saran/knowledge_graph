"""PRD section 7: a screen on the fixtures takes less than 5 s at p95."""

import time

import pytest
from e2e import cli_process, screen_gold

from ubo_sentinel.agents.base import ScreenRequest
from ubo_sentinel.agents.supervisor import Supervisor
from ubo_sentinel.audit import log as audit_log
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.cli.eval_cmd import LATENCY_P95_TARGET_SECONDS, p95

pytestmark = pytest.mark.integration


def test_every_scenario_is_screened_within_the_budget(fresh_db, ontology, gold):
    """As the command screens: its own read-only connection, a real log that grows."""
    fresh_db("t0")
    supervisor = Supervisor(ontology, AuditLog())
    seconds = {}
    for scenario in gold:
        started = time.perf_counter()
        screen_gold(supervisor, scenario, "fixtures")
        seconds[scenario.id] = time.perf_counter() - started
    slowest = sorted(seconds.items(), key=lambda item: -item[1])[:5]
    assert p95(list(seconds.values())) < LATENCY_P95_TARGET_SECONDS, slowest


def test_a_screen_reads_the_log_once(fresh_db, ontology, monkeypatch):
    """Reading the log is linear in its length; a screen must not do it more than once."""
    fresh_db("t0")
    log = AuditLog()
    supervisor = Supervisor(ontology, log)
    for query in ("Vostrek Maritime LLC", "Halcyon Ridge Minerals Ltd"):
        supervisor.run(ScreenRequest(query=query))

    reads = []
    original = audit_log.AuditLog._read

    def counted(self):
        reads.append(self.path)
        return original(self)

    monkeypatch.setattr(audit_log.AuditLog, "_read", counted)
    assert supervisor.run(ScreenRequest(query="Acme Trading FZE")).is_new
    assert len(reads) == 1
    reads.clear()
    assert not supervisor.run(ScreenRequest(query="Acme Trading FZE")).is_new
    assert len(reads) == 1


@pytest.mark.slow
def test_the_command_answers_within_the_budget_from_a_cold_start(fresh_db):
    """Step 7: `ubo screen` in a new process, imports and all."""
    fresh_db("t0")
    started = time.perf_counter()
    code, output = cli_process("screen", "Acme Trading FZE")
    elapsed = time.perf_counter() - started
    assert code == 0, output
    assert elapsed < LATENCY_P95_TARGET_SECONDS, f"{elapsed:.2f}s"
