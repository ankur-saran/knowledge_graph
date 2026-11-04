"""Investigations in the review UI (Step 14.8): the panel helpers, the graph layer and the
page, with the same masking as the CLI. The page runs in-process through AppTest."""

import _socket
import socket
from contextlib import contextmanager
from pathlib import Path

import pytest
from conftest import INV
from e2e import person_pii
from streamlit.testing.v1 import AppTest

from ubo_sentinel.agents.base import ScreenRequest
from ubo_sentinel.agents.investigator import run_investigation
from ubo_sentinel.agents.supervisor import Supervisor, utc_now
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.models import EntityRow, read_rows
from ubo_sentinel.ui import graph_viz, panels

APP = str(Path(__file__).resolve().parents[2] / "src" / "ubo_sentinel" / "ui" / "app.py")
DESIGNATED = "lei:FXI03DESIG0000000000"
PII = [*person_pii(read_rows(INV / "entities.csv", EntityRow)), "Casimir Ondrejka"]


@pytest.fixture(autouse=True)
def loopback_socketpair(monkeypatch):
    """As in test_ui.py: AppTest's asyncio self-pipe is the one socket let through."""
    pair = socket.socketpair

    def loopback_pair(*args, **kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(socket.socket, "connect", _socket.socket.connect)
            return pair(*args, **kwargs)

    monkeypatch.setattr(socket, "socketpair", loopback_pair)


@pytest.fixture
def decided(fresh_db, ontology):
    """The designated company of i03 screened by an analyst, and one investigation of it."""
    _, inv_set = fresh_db("t0_inv")
    log = AuditLog()
    request = ScreenRequest(target_id=DESIGNATED, snapshot=inv_set, actor="a.analyst")
    decision = Supervisor(ontology, log).run(request).decision
    found, _ = run_investigation(
        log, ontology, decision.decision_id, "Who else is here?", "a.analyst", "analyst",
        utc_now(),
    )  # fmt: skip
    return decision, found, log


# --- the helpers --------------------------------------------------------------------------------


def test_the_investigations_of_a_decision_are_read_from_the_log(decided):
    decision, found, log = decided
    assert panels.investigations_of(log.records(), decision.decision_id) == [found]
    assert panels.investigations_of(log.records(), "another") == []
    (row,) = panels.investigation_rows(panels.investigations_of(log.records()))
    assert row["investigation"] == found.investigation_id and row["model"] == "first pass only"
    assert "question" not in row


def test_each_role_sees_the_investigation_as_it_sees_the_graph(decided, ontology):
    decision, found, _ = decided
    analyst_text, analyst_graph = panels.investigation_view(found, decision, "analyst", ontology)
    shown = analyst_text + analyst_graph.text()
    assert [value for value in PII if value in shown] == [] and "person:" in shown
    reviewer_text, _ = panels.investigation_view(found, decision, "reviewer", ontology)
    assert "Dmitri Volkanov" in reviewer_text


def test_the_findings_are_drawn_as_their_own_layer(decided, ontology):
    decision, found, _ = decided
    _, spec = panels.investigation_view(found, decision, "analyst", ontology)
    layer = [edge for edge in spec.edges if edge.kind == "INVESTIGATION"]
    assert layer and all(edge.upper_id == DESIGNATED for edge in layer)
    assert {edge.label for edge in layer} == {"investigation: SHARED_ADDRESS"}
    outside = {node.id: node for node in spec.nodes if "INVESTIGATION" in node.label}
    assert "fx_list:i06-owner" in outside and outside["fx_list:i06-owner"].state == "outside"
    drawn = graph_viz._vis_data(spec)["edges"]
    assert {tuple(e["dashes"]) for e in drawn if e["id"].startswith("investigation:")} == {(8, 4)}


def test_the_layer_escapes_a_name_like_the_rest_of_the_graph():
    spec = graph_viz.GraphSpec(nodes=[], edges=[])
    layered = graph_viz.with_investigation(
        spec, [("SHARED_ADDRESS", ["a", "b"])], lambda i: (i, "</script><b>x</b>")
    )
    page = graph_viz.to_html(layered)
    assert "</script><b>" not in page.split("<script>", 2)[2]


def test_a_view_the_render_guardrail_stops_is_not_shown(decided, ontology, monkeypatch):
    decision, found, _ = decided
    monkeypatch.setattr(panels, "check_render", lambda *args: ["a leak"])
    assert panels.investigation_view(found, decision, "reviewer", ontology) is None


def test_the_model_is_asked_outside_the_lock_and_the_append_inside(decided, ontology, monkeypatch):
    decision, _, log = decided
    held = []

    @contextmanager
    def lock():
        held.append(True)
        yield
        held.pop()

    original = panels.investigate

    def watched(*args, **kwargs):
        assert not held, "no model call under the process lock"
        return original(*args, **kwargs)

    monkeypatch.setattr(panels, "investigate", watched)
    made = panels.run_investigation(log, ontology, decision, "Anything else?", "reviewer",
                                    "r.reviewer", lock())  # fmt: skip
    assert log.records()[-1]["decision_id"] == made.event_id and held == []


def test_a_role_that_may_not_investigate_or_replay_is_refused(decided, ontology):
    decision, found, log = decided
    with pytest.raises(PermissionError):
        panels.run_investigation(log, ontology, decision, "q", "auditor", "aud", lock=None)
    with pytest.raises(PermissionError):
        panels.replay_investigation(log, ontology, "analyst", found.investigation_id)
    report = panels.replay_investigation(log, ontology, "auditor", found.investigation_id)
    assert report.identical


# --- the page -----------------------------------------------------------------------------------


def page(role, actor=None):
    app = AppTest.from_file(APP, default_timeout=120)
    app.session_state["role"] = role
    app.session_state["actor"] = actor or role
    app.session_state["snapshot"] = "latest"
    return app.run()


def every_text(app):
    parts = [e.value for kind in ("markdown", "code", "caption", "info", "error", "warning",
                                  "text", "success") for e in getattr(app, kind)]  # fmt: skip
    parts += [frame.value.to_string() for frame in app.dataframe]
    return "\n".join(map(str, parts))


def test_the_reviewer_reads_and_runs_an_investigation_and_records_it_read(decided):
    decision, found, log = decided
    app = page("reviewer", "r.reviewer")
    assert not app.exception
    assert app.selectbox(key="review-investigation-pick").value == found.investigation_id
    assert "Dmitri Volkanov" in every_text(app)
    app.text_input(key="review-question").input("Who directs it?")
    app.run()
    app.button(key="review-investigate").click()
    app.run()
    assert not app.exception
    assert len(panels.investigations_of(AuditLog().records(), decision.decision_id)) == 2
    app.multiselect(key="considered").set_value([found.investigation_id])
    app.button(key="review-submit").click()
    app.run()
    review = AuditLog().records()[-1]
    assert review["payload"]["considered_investigations"] == [found.investigation_id]


def test_the_auditor_lists_and_replays_investigations_read_only(decided):
    _, found, _ = decided
    app = page("auditor")
    assert not app.exception
    assert found.investigation_id in every_text(app)
    app.button(key="replay-investigation").click()
    app.run()
    assert "Replay identical" in [s.value for s in app.success]
    assert not [b for b in app.button if b.key and b.key.endswith("-investigate")]
