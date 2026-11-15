"""Step 15 in the review UI: the Extraction review panel and document edges on the
evidence graph. The page runs in-process through AppTest."""

import _socket
import socket
from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from ubo_sentinel.agents.base import ScreenRequest
from ubo_sentinel.agents.supervisor import Supervisor, utc_now
from ubo_sentinel.audit.extraction_review import proposal_states
from ubo_sentinel.audit.log import AuditLog, var_dir
from ubo_sentinel.graph.views import mask_decision
from ubo_sentinel.pipeline.bronze import resolve_set
from ubo_sentinel.pipeline.db import connect
from ubo_sentinel.pipeline.extraction.run import run_extraction
from ubo_sentinel.pipeline.gold import build_gold
from ubo_sentinel.pipeline.loaders.document_loader import ingest_documents
from ubo_sentinel.pipeline.transformer import build_silver
from ubo_sentinel.ui import graph_viz, panels

APP = str(Path(__file__).resolve().parents[2] / "src" / "ubo_sentinel" / "ui" / "app.py")
ACME = "lei:FXS01TARGET000000000"
VOSTREK = "lei:FXS02TARGET000000000"
REGISTER = Path("eval/extraction/doc-01-register.txt")
NARRATIVE = Path("eval/extraction/doc-02-narrative.txt")


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
def extracted(fresh_db, ontology):
    (t0,) = fresh_db("t0")
    log = AuditLog()
    for path in (NARRATIVE, REGISTER):
        run_extraction(log, ontology, path, ACME, t0, "a.analyst", "analyst", utc_now(), var_dir())
    states = proposal_states(log.records())
    by_owner = {s.proposal.edge.owner_name: s.proposal.proposal_id for s in states.values()}
    return log, t0, by_owner


def page(role, actor=None):
    app = AppTest.from_file(APP, default_timeout=120)
    app.session_state["role"] = role
    app.session_state["actor"] = actor or role
    return app.run()


def test_the_panel_is_the_reviewers_and_masks_for_any_other(extracted, ontology):
    log, _, by_owner = extracted
    assert "extraction" in panels.allowed_panels(ontology, "reviewer")
    assert "extraction" not in panels.allowed_panels(ontology, "analyst")
    with pytest.raises(PermissionError):
        panels.extraction_items(log.records(), "analyst", ontology)
    items = {
        item.proposal_id: item
        for item in panels.extraction_items(log.records(), "reviewer", ontology)
    }
    vostrek = items[by_owner["Vostrek Maritime L.L.C."]]
    assert "«Vostrek Maritime L.L.C. holds 55 %" in vostrek.page
    assert vostrek.owner_candidates == [VOSTREK] and vostrek.subject_id == ACME
    # Held to the reviewer's view: one that masks persons would not be shown Tobias.
    ontology.roles["reviewer"].masked_fields["Person"] = ["legal_name", "aliases"]
    try:
        masked = {
            i.proposal_id: i for i in panels.extraction_items(log.records(), "reviewer", ontology)
        }
    finally:
        ontology.roles["reviewer"].masked_fields.clear()
    tobias = masked[by_owner["Tobias Wrenfield"]]
    assert tobias.page is None and "withheld" in tobias.lines[0]


def test_a_reviewer_accepts_and_revokes_on_the_page(extracted):
    log, _, by_owner = extracted
    app = page("reviewer", "r.reviewer")
    assert not app.exception
    proposal = by_owner["Vostrek Maritime L.L.C."]
    picks = app.selectbox(key="extraction-proposal")
    picks.set_value(next(i for i, o in enumerate(picks.options) if proposal in o)).run()
    app.selectbox(key="extraction-owner").set_value(VOSTREK)
    app.date_input(key="extraction-as-of").set_value(date(2026, 9, 30))
    app.button(key="extraction-accept").click()
    app.run()
    assert not app.exception and not app.error
    assert proposal_states(AuditLog().records())[proposal].status == "ACCEPTED"
    picks = app.selectbox(key="extraction-proposal")
    picks.set_value(next(i for i, o in enumerate(picks.options) if proposal in o)).run()
    app.text_input(key="extraction-revoke-reason").input("withdrawn")
    app.button(key="extraction-revoke").click()
    app.run()
    assert proposal_states(AuditLog().records())[proposal].status == "REVOKED"


def test_the_extractor_is_refused_and_a_rejection_needs_a_reason(extracted):
    log, _, by_owner = extracted
    app = page("reviewer", "a.analyst")
    app.button(key="extraction-reject").click()
    app.run()
    assert any("Refused" in e.value for e in app.error)
    assert len(AuditLog().records()) == len(log.records())


def test_a_document_edge_is_drawn_apart_on_the_evidence_graph(extracted, ontology):
    log, t0, by_owner = extracted
    from ubo_sentinel.audit.extraction_review import review_proposal

    review_proposal(log, ontology, by_owner["Vostrek Maritime L.L.C."], "ACCEPTED", "r", "reviewer",
                    utc_now(), var_dir(), as_of=date(2026, 9, 30), owner_id=VOSTREK)  # fmt: skip
    con = connect()
    try:
        published = ingest_documents(con).snapshot_set
        snapshot_set = resolve_set(con, published.snapshot_set_id)
        build_silver(con, snapshot_set, ontology)
        build_gold(con, snapshot_set)
    finally:
        con.close()
    decision = (
        Supervisor(ontology, log)
        .run(ScreenRequest(target_id=ACME, snapshot=published.snapshot_set_id, actor="a"))
        .decision
    )
    spec = graph_viz.evidence_graph(mask_decision(decision, "reviewer", ontology), scope="checked")
    (edge,) = [e for e in spec.edges if e.from_document]
    assert "(document)" in edge.label
    drawn = graph_viz._vis_data(spec)
    colours = {e["id"]: e["color"]["color"] for e in drawn["edges"]}
    assert colours[edge.id] == graph_viz.DOCUMENT_COLOUR
    assert any("document" in mark for mark, _ in graph_viz.LEGEND)
