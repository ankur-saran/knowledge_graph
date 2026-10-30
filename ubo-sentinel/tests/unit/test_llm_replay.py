"""Replay of recorded LLM output: the input rebuilt, the guardrail run again, no model."""

import pytest
from e2e import cli
from llm_fakes import FakeClient, gold_decisions, good_prose
from test_match_adjudicator import good_answer

from ubo_sentinel.agents import llm_prose
from ubo_sentinel.agents import match_adjudicator as adjudicator
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.llm import annotation as annotations
from ubo_sentinel.llm import replay
from ubo_sentinel.llm.replay import UnknownRecord, replay_annotation

MODEL = "claude-haiku-5-5"


@pytest.fixture
def decisions(fresh_con, ontology, gold):
    return gold_decisions(fresh_con, ontology, gold, ["SCEN-03", "SCEN-14"])[1]


def _record(decision, ontology, annotate, output, fail=False):
    log = AuditLog()
    client = FakeClient(output, fail=fail)
    found, _ = annotate(decision, ontology, client, MODEL, 1000)
    annotations.record(log, ontology, found, "someone", "reviewer", decision.created_at)
    return log, found, client


def test_a_recorded_annotation_replays_identical_without_a_model(decisions, ontology):
    for scenario, annotate, output in (
        ("SCEN-03", llm_prose.annotate_prose, good_prose),
        ("SCEN-14", adjudicator.annotate_matches, good_answer),
    ):
        log, found, client = _record(decisions[scenario], ontology, annotate, output)
        asked = len(client.requests)
        report = replay_annotation(log, ontology, found.annotation_id)
        assert report.identical, report.detail
        assert (report.feature, report.recorded_status) == (found.feature, "ACCEPTED")
        # The model is never asked again.
        assert len(client.requests) == asked == 1


def test_a_rejected_or_unavailable_answer_replays_with_its_faults(decisions, ontology):
    decision = decisions["SCEN-03"]
    bad = {"sentences": [{"claim": 0, "text": "safe to clear"}]}
    log, rejected, _ = _record(decision, ontology, llm_prose.annotate_prose, bad)
    assert rejected.status == "REJECTED" and rejected.faults
    assert replay_annotation(log, ontology, rejected.annotation_id).identical
    log, missing, _ = _record(decision, ontology, llm_prose.annotate_prose, None, fail=True)
    assert missing.status == "UNAVAILABLE"
    assert replay_annotation(log, ontology, missing.annotation_id).identical


def test_each_verdict_names_what_moved(decisions, ontology, monkeypatch):
    log, found, _ = _record(decisions["SCEN-03"], ontology, llm_prose.annotate_prose, good_prose)
    feature = replay.FEATURES["prose"]

    def verdict():
        return replay_annotation(log, ontology, found.annotation_id).verdict

    # Another prompt version: this code cannot say what that prompt was sent.
    monkeypatch.setitem(
        replay.FEATURES, "prose", replay._Feature(2, *list(vars(feature).values())[1:])
    )
    assert verdict() == "NOT_REPLAYABLE"
    # The input this code builds is not the one the model read.
    built = replay._Feature(
        feature.prompt_version, feature.system, feature.schema,
        lambda d, o: feature.build(d, o) | {"extra": 1}, feature.check,
    )  # fmt: skip
    monkeypatch.setitem(replay.FEATURES, "prose", built)
    assert verdict() == "INPUT_DIFFERS"
    # The guardrail now finds a fault it did not.
    checked = replay._Feature(
        feature.prompt_version, feature.system, feature.schema, feature.build,
        lambda *args: ["a new kind of fault"],
    )  # fmt: skip
    monkeypatch.setitem(replay.FEATURES, "prose", checked)
    assert verdict() == "GUARDRAIL_DIFFERS"
    monkeypatch.delitem(replay.FEATURES, "prose")
    assert verdict() == "NOT_REPLAYABLE"


def test_an_unknown_annotation_is_named(ontology):
    with pytest.raises(UnknownRecord, match="No annotation"):
        replay_annotation(AuditLog(), ontology, "nothing")


def test_the_command(decisions, ontology, monkeypatch):
    log, found, _ = _record(decisions["SCEN-03"], ontology, llm_prose.annotate_prose, good_prose)
    result = cli("audit", "replay-annotation", found.annotation_id)
    assert result.exit_code == 0 and "Replay identical" in result.output
    as_json = cli("audit", "replay-annotation", found.annotation_id, "--json")
    assert '"verdict": "IDENTICAL"' in as_json.output
    assert cli("audit", "replay-annotation", "nothing").exit_code == 2
    analyst = cli("audit", "replay-annotation", found.annotation_id, "--role", "analyst")
    assert analyst.exit_code == 1
    monkeypatch.setitem(
        replay.FEATURES,
        "prose",
        replay._Feature(9, *list(vars(replay.FEATURES["prose"]).values())[1:]),
    )
    refused = cli("audit", "replay-annotation", found.annotation_id)
    assert refused.exit_code == 4 and "NOT_REPLAYABLE" in refused.output
    monkeypatch.setitem(
        replay.FEATURES,
        "prose",
        replay._Feature(
            llm_prose.PROMPT_VERSION, llm_prose.SYSTEM, llm_prose.SCHEMA,
            lambda d, o: {}, llm_prose.check_slotted,
        ),
    )  # fmt: skip
    differs = cli("audit", "replay-annotation", found.annotation_id)
    assert differs.exit_code == 1 and "INPUT_DIFFERS" in differs.output
