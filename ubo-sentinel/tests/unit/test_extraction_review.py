"""Step 15: the run recorded once, a ruling per proposal held to the log's rules, and an
extraction replayed from the stored text with each verdict made to happen."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from llm_fakes import FakeClient

from ubo_sentinel.audit import extraction_review as review
from ubo_sentinel.audit.log import AuditLog, var_dir
from ubo_sentinel.llm import replay
from ubo_sentinel.llm.config import ExtractBudget
from ubo_sentinel.models.extraction import NewEntity
from ubo_sentinel.pipeline.extraction import llm_extract
from ubo_sentinel.pipeline.extraction.document import documents_dir
from ubo_sentinel.pipeline.extraction.run import extractions_of, run_extraction

AT = datetime(2026, 10, 1, tzinfo=UTC)
ACME = "lei:FXS01TARGET000000000"
VOSTREK = "lei:FXS02TARGET000000000"
TEXT = (
    "Ownership of Acme Trading FZE.\n"
    "Vostrek Maritime L.L.C. holds 55 % of the issued shares of Acme Trading FZE."
    " Ivo Marchetti holds 20 % of Acme Trading FZE."
    " Tobias Wrenfield holds an indirect 10 % of Acme Trading FZE through Vostrek Maritime L.L.C."
)


@pytest.fixture
def setup(fresh_con, ontology, tmp_path):
    con, (t0,) = fresh_con("t0")
    path = tmp_path / "ownership.txt"
    path.write_text(TEXT, encoding="utf-8")
    log = AuditLog()

    def extract(actor="ana", role="analyst", **options):
        return run_extraction(
            log, ontology, path, ACME, t0, actor, role, AT, var_dir(), con=con, **options
        )

    found, recorded = extract()
    by_owner = {p.edge.owner_name: p.proposal_id for p in found.proposals}
    return log, con, found, recorded, by_owner, extract, path


def rule(setup, ontology, owner, action="ACCEPTED", actor="rita", **options):
    log, con, _, _, by_owner, _, _ = setup
    return review.review_proposal(
        log, ontology, by_owner[owner], action, actor, "reviewer", AT, var_dir(), con=con, **options
    )


def test_an_extraction_is_recorded_once_and_its_proposals_wait(setup, ontology):
    log, _, found, recorded, by_owner, extract, _ = setup
    assert recorded and extract()[1] is False
    assert extractions_of(log.records()) == [found]
    assert {p.label for p in found.proposals} == {"NEW", "INDIRECT_INFO"}
    waiting = [state.proposal.proposal_id for state in review.queue(log.records())]
    # Information is shown, never queued for a ruling.
    assert sorted(waiting) == sorted(
        [by_owner["Vostrek Maritime L.L.C."], by_owner["Ivo Marchetti"]]
    )


def test_a_role_that_may_not_extract_or_write_is_refused_before_anything(setup):
    _, _, _, _, _, extract, _ = setup
    for role in ("auditor", "engineer"):
        with pytest.raises(PermissionError):
            extract(role=role)


def test_an_investigation_named_must_be_on_record(setup):
    _, _, _, _, _, extract, _ = setup
    with pytest.raises(LookupError, match="No investigation"):
        extract(investigation_id="0123456789abcdef")


def test_an_acceptance_names_both_ends_by_their_records(setup, ontology):
    log, *_ = setup
    rule(setup, ontology, "Vostrek Maritime L.L.C.", owner_id=VOSTREK, as_of=date(2026, 9, 30))
    rule(
        setup, ontology, "Ivo Marchetti", as_of=date(2026, 9, 30),
        owner_new=NewEntity(name="Ivo Marchetti", entity_type="Person", jurisdiction="IT"),
    )  # fmt: skip
    accepted = {s.proposal.edge.owner_name: s for s in review.accepted(log.records())}
    vostrek = accepted["Vostrek Maritime L.L.C."].acceptance
    # The record that describes the entity: the registry's, before the list's.
    assert vostrek.owner.ref == "fx_registry:s02-target" and vostrek.asset.entity_id == ACME
    ivo = accepted["Ivo Marchetti"].acceptance
    assert ivo.owner.new_entity.entity_type == "Person" and ivo.owner.ref.startswith("document:")
    assert review.queue(log.records()) == []


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"owner_id": VOSTREK}, "effective date"),
        ({"as_of": date(2026, 9, 30)}, "Name the owner"),
        ({"as_of": date(2026, 9, 30), "owner_id": "lei:NOSUCH"}, "No entity"),
        ({"as_of": date(2026, 9, 30), "owner_id": ACME}, "one entity"),
        ({"as_of": date(2026, 9, 30), "owner_id": VOSTREK, "pct": Decimal("60")}, "guardrail"),
    ],
)
def test_an_acceptance_that_cannot_be_made_as_asked_is_refused(setup, ontology, options, message):
    with pytest.raises(review.ExtractionReviewError, match=message):
        rule(setup, ontology, "Vostrek Maritime L.L.C.", **options)


def test_an_edit_is_held_to_the_quote_and_recorded_before_and_after(setup, ontology, tmp_path):
    log, *_ = setup
    # The quote writes 55 %: an edit to 55.0 stands, as does a change of nothing.
    rule(setup, ontology, "Vostrek Maritime L.L.C.", owner_id=VOSTREK, as_of=date(2026, 9, 30),
         pct=Decimal("55.0"))  # fmt: skip
    (state,) = review.accepted(log.records())
    assert state.acceptance.edits == {} and state.acceptance.pct == Decimal("55")


def test_the_log_refuses_a_ruling_that_cannot_follow(setup, ontology):
    log, *_ = setup
    vostrek = {"owner_id": VOSTREK, "as_of": date(2026, 9, 30)}
    with pytest.raises(ValueError, match="actor who extracted it"):
        rule(setup, ontology, "Vostrek Maritime L.L.C.", actor=" ANA ", **vostrek)
    with pytest.raises(ValueError, match="without a reason"):
        rule(setup, ontology, "Ivo Marchetti", "REJECTED")
    with pytest.raises(ValueError, match="not accepted"):
        rule(setup, ontology, "Ivo Marchetti", "REVOKED", reason="wrong")
    with pytest.raises(ValueError, match="information only"):
        rule(setup, ontology, "Tobias Wrenfield", owner_id=VOSTREK, as_of=date(2026, 9, 30))
    rule(setup, ontology, "Vostrek Maritime L.L.C.", **vostrek)
    with pytest.raises(ValueError, match="already ACCEPTED"):
        rule(setup, ontology, "Vostrek Maritime L.L.C.", "REJECTED", reason="no")
    rule(
        setup, ontology, "Vostrek Maritime L.L.C.", "REVOKED", reason="the register was superseded"
    )
    assert review.accepted(log.records()) == []
    with pytest.raises(ValueError, match="already REVOKED"):
        rule(setup, ontology, "Vostrek Maritime L.L.C.", **vostrek)
    with pytest.raises(LookupError):
        review.review_proposal(
            log, ontology, "nosuch", "REJECTED", "rita", "reviewer", AT, var_dir()
        )
    with pytest.raises(PermissionError):
        review.review_proposal(log, ontology, "x", "REJECTED", "a", "analyst", AT, var_dir())
    assert log.verify(ontology).intact


def test_the_ledger_refuses_a_hand_made_ruling(setup, ontology):
    log, *_ = setup
    for decision_id, payload, problem in (
        ("proposal:nosuch", {"action": "REJECTED"}, "not extracted"),
        ("nosuch", {"action": "REJECTED"}, "does not name its proposal"),
    ):
        with pytest.raises(ValueError, match=problem):
            log.append(ontology=ontology, decision_id=decision_id, event_type="EXTRACTION_REVIEWED",
                       actor="rita", role="reviewer", created_at=AT, payload=payload, note="x")  # fmt: skip
    _, _, found, _, by_owner, _, _ = setup
    target = f"proposal:{by_owner['Ivo Marchetti']}"
    with pytest.raises(ValueError, match="without an action"):
        log.append(ontology=ontology, decision_id=target, event_type="EXTRACTION_REVIEWED",
                   actor="rita", role="reviewer", created_at=AT, payload={}, note="x")  # fmt: skip
    with pytest.raises(ValueError, match="states no edge"):
        log.append(ontology=ontology, decision_id=target, event_type="EXTRACTION_REVIEWED",
                   actor="rita", role="reviewer", created_at=AT, payload={"action": "ACCEPTED"})  # fmt: skip
    # An annotation or an investigation is of a decision, never of a proposal or a run.
    with pytest.raises(ValueError, match="not recommended"):
        log.append(ontology=ontology, decision_id=found.event_id, event_type="ANNOTATED",
                   actor="rita", role="reviewer", created_at=AT, payload={"annotation_id": "a"})  # fmt: skip


def test_a_new_entity_is_written_name_type_and_country():
    assert review.parse_new_entity("Ivo Marchetti;Person;IT") == NewEntity(
        name="Ivo Marchetti", entity_type="Person", jurisdiction="IT"
    )
    assert review.parse_new_entity("HoldCo;LegalEntity").jurisdiction is None
    for bad in ("Ivo", ";Person", "Ivo;Robot"):
        with pytest.raises(review.ExtractionReviewError):
            review.parse_new_entity(bad)


# --- replay ---------------------------------------------------------------------------------


def test_every_kind_of_extraction_replays_identically(setup, ontology, monkeypatch):
    log, con, found, _, _, extract, _ = setup
    assert replay.replay_extraction(log, found.extraction_id, var_dir(), con).identical
    budget = ExtractBudget(model="m", max_pages=40, max_tokens=10**6)
    output = {"holdings": []}
    asked, _ = extract(actor="ana", client=FakeClient(output), budget=budget, consent=True)
    failed, _ = extract(actor="ana", client=FakeClient(fail=True), budget=budget, consent=True)
    for run_ in (asked, failed):
        report = replay.replay_extraction(log, run_.extraction_id, var_dir(), con)
        assert report.identical, report.detail


def test_each_replay_verdict_is_made_to_happen(setup, monkeypatch):
    log, con, found, _, _, extract, _ = setup
    with pytest.raises(replay.UnknownRecord):
        replay.replay_extraction(log, "nosuch", var_dir(), con)

    def verdict(extraction):
        return replay.replay_extraction(log, extraction.extraction_id, var_dir(), con).verdict

    budget = ExtractBudget(model="m", max_pages=40, max_tokens=10**6)
    asked, _ = extract(client=FakeClient({"holdings": []}), budget=budget, consent=True)
    with monkeypatch.context() as patch:
        patch.setattr(llm_extract, "SYSTEM", llm_extract.SYSTEM + " ")
        assert verdict(asked) == "NOT_REPLAYABLE"
    # The stored text changed.
    pages = documents_dir(var_dir()) / f"{found.document_sha256}.pages.json"
    kept = pages.read_bytes()
    pages.write_bytes(kept.replace(b"Ownership", b"Ownersh1p"))
    assert verdict(found) == "DOCUMENT_CHANGED"
    pages.unlink()
    assert verdict(found) == "DOCUMENT_CHANGED"
    pages.write_bytes(kept)
    # A request that is not the one recorded: other persons pseudonymised.
    with monkeypatch.context() as patch:
        patch.setattr(
            "ubo_sentinel.pipeline.extraction.run.persons_of", lambda store: {"p": ["Ownership"]}
        )
        assert verdict(asked) == "INPUT_DIFFERS"
    # The guardrail now finds otherwise.
    with monkeypatch.context() as patch:
        patch.setattr(
            "ubo_sentinel.pipeline.extraction.run.check_edges",
            lambda edges, pages: ([], ["QUOTE_NOT_FOUND"] * len(edges)),
        )
        assert verdict(found) == "GUARDRAIL_DIFFERS"
    assert verdict(found) == "IDENTICAL"
