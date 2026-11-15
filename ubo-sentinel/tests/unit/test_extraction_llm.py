"""Step 15: a model reads a document only with consent, persons pseudonymised; its
answers are restored, checked and labelled like the parser's."""

from decimal import Decimal

import pytest
from e2e import person_pii
from llm_fakes import FakeClient

from ubo_sentinel.graph import queries
from ubo_sentinel.graph.store import GraphStore
from ubo_sentinel.graph.views import pseudonym_for
from ubo_sentinel.llm.client import RecordedClient
from ubo_sentinel.llm.config import ExtractBudget
from ubo_sentinel.llm.egress import DocumentEgress, EgressRefused
from ubo_sentinel.models.canonical import canonical_json
from ubo_sentinel.models.extraction import Suggestion
from ubo_sentinel.models.graph import OwnerEdge
from ubo_sentinel.models.relationship import Relationship
from ubo_sentinel.pipeline.extraction import llm_extract, run
from ubo_sentinel.pipeline.extraction.document import DocumentText, normalise_text

ACME = "lei:FXS01TARGET000000000"
TOBIAS = "fx_list:s01-minority"
BUDGET = ExtractBudget(model="claude-sonnet-5-5", max_pages=40, max_tokens=200000)
PASSIVE = (
    "Capital of Acme Trading FZE.\n60 % of the issued shares of Acme Trading FZE are held by"
    " Quillon Harbour Group Ltd, and the remaining 40 % by Tobias Wrenfield."
)


def document(*pages):
    pages = [normalise_text(page) for page in pages]
    return DocumentText(
        sha256="ab" * 32, file_name="doc.txt", suffix=".txt", extractor="text", pages=pages
    )


def holding(owner, pct, quote, page=1, **extra):
    return {
        "owner_name": owner, "asset_name": "Acme Trading FZE", "rel_type": "OWNS",
        "basis": "shareholding", "pct": pct, "holding": "direct", "interest": "equity",
        "share_class": None, "quote": quote, "page": page, "as_of_hint": None,
    } | extra  # fmt: skip


@pytest.fixture
def store(fresh_con):
    con, (t0,) = fresh_con("t0")
    with GraphStore.open(t0, con) as opened:
        yield opened


def test_a_document_leaves_only_with_consent_and_known_persons_as_tokens():
    with pytest.raises(EgressRefused):
        DocumentEgress(["Tobias Wrenfield holds 40 %."], {TOBIAS: ["Tobias Wrenfield"]}, False)
    egress = DocumentEgress(
        ["TOBIAS WRENFIELD and Tobias Wrenfield hold 40 %; Wrenfielder is someone else.", "Tob"],
        {TOBIAS: ["Tobias Wrenfield", "Tob"]},
        True,
    )
    alias = pseudonym_for(TOBIAS, "Person")
    assert "Tobias" not in egress.pages[0] and "TOBIAS" not in egress.pages[0]
    assert f"[{alias}~1]" in egress.pages[0] and f"[{alias}~2]" in egress.pages[0]
    assert "Wrenfielder" in egress.pages[0] and egress.pages[1] == "Tob"  # too short a name
    assert egress.restore(egress.pages[0]) == (
        "TOBIAS WRENFIELD and Tobias Wrenfield hold 40 %; Wrenfielder is someone else."
    )
    assert egress.replaced == 2


def test_the_model_reads_no_person_of_the_set_and_its_answer_is_restored(store, entities):
    def answer(request):
        page = request.data["pages"][0]["text"]
        tobias = page[page.index("[person:") : page.index("]") + 1]
        return {
            "holdings": [
                holding("Quillon Harbour Group Ltd", "60",
                        "60 % of the issued shares of Acme Trading FZE are held by Quillon Harbour Group Ltd"),
                holding(tobias, "40 %", f"the remaining 40 % by {tobias}"),
            ]
        }  # fmt: skip

    client = FakeClient(answer)
    found = run.extract(store, document(PASSIVE), ACME, client, BUDGET, consent=True)
    sent = canonical_json([r.model_dump(mode="json") for r in client.requests]).decode()
    assert [value for value in person_pii(entities) if value in sent] == []
    assert found.egress == "document_consented" and found.model == BUDGET.model
    got = {(p.origin, p.edge.owner_name, p.edge.pct, p.label) for p in found.proposals}
    assert got == {
        ("model", "Quillon Harbour Group Ltd", Decimal("60"), "CONFIRMS"),
        ("model", "Tobias Wrenfield", Decimal("40"), "CONFIRMS"),
    }
    tobias = next(p for p in found.proposals if p.edge.owner_name == "Tobias Wrenfield")
    assert tobias.owner_candidates[0].entity_id == TOBIAS and tobias.existing_pct == Decimal("40")
    assert found.complete and found.faults == [] and found.budgets["max_pages"] == 40


def test_what_a_model_writes_is_checked_like_the_parsers(store):
    text = "Quillon Harbour Group Ltd holds 45 % of the issued shares of Acme Trading FZE."
    output = {
        "holdings": [
            # The parser found this one too: kept once, as the parser's.
            holding("Quillon Harbour Group Ltd", "45", text.rstrip(".")),
            holding("Vostrek Maritime L.L.C.", "51", "Vostrek holds 51 %"),  # not in the text
            {"owner_name": "x"},  # malformed
            holding("Quillon Harbour Group Ltd", "fifty", text),  # not a figure
        ]
    }
    found = run.extract(store, document(text), ACME, FakeClient(output), BUDGET, consent=True)
    (only,) = found.proposals
    assert (only.origin, only.label, only.existing_pct) == (
        "first_pass",
        "CONFLICTS",
        Decimal("60"),
    )
    assert found.faults == sorted(["MALFORMED_HOLDING", "MALFORMED_HOLDING", "QUOTE_NOT_FOUND"])
    assert found.dropped == 1


def test_a_model_is_not_asked_without_consent_and_a_failure_keeps_the_first_pass(store):
    text = "Quillon Harbour Group Ltd holds 60 % of the issued shares of Acme Trading FZE."
    client = FakeClient({"holdings": []})
    found = run.extract(store, document(text), ACME, client, BUDGET, consent=False)
    assert client.requests == [] and found.egress == "none" and found.model is None
    failed = run.extract(store, document(text), ACME, FakeClient(fail=True), BUDGET, consent=True)
    assert [p.origin for p in failed.proposals] == ["first_pass"]
    assert failed.faults == ["MODEL_UNAVAILABLE"] and failed.pages_not_read == [1]
    assert not failed.complete


def test_budgets_name_the_pages_not_read(store):
    pages = [f"Page {n}." for n in range(1, 7)]
    found = run.extract(
        store, document(*pages), ACME, FakeClient({"holdings": []}),
        ExtractBudget(model="m", max_pages=4, max_tokens=10**6), consent=True,
    )  # fmt: skip
    assert [call.pages for call in found.calls] == [[1, 2, 3, 4]]
    assert found.pages_not_read == [5, 6] and not found.complete
    # The fake spends 15 tokens a call: the second window is not asked.
    spent = run.extract(
        store, document(*pages), ACME, FakeClient({"holdings": []}),
        ExtractBudget(model="m", max_pages=40, max_tokens=10), consent=True,
    )  # fmt: skip
    assert len(spent.calls) == 1 and spent.pages_not_read == [5, 6]


def test_a_recorded_run_replays_offline_as_the_same_extraction(store, tmp_path):
    output = {
        "holdings": [
            holding("Quillon Harbour Group Ltd", "60", PASSIVE.split("\n")[1].split(",")[0])
        ]
    }
    recorder = RecordedClient(tmp_path, FakeClient(output))
    first = run.extract(store, document(PASSIVE), ACME, recorder, BUDGET, consent=True)
    again = run.extract(
        store, document(PASSIVE), ACME, RecordedClient(tmp_path), BUDGET, consent=True
    )
    assert first == again and first.proposals and len(list(tmp_path.glob("extract-*.json"))) == 1


def test_labels_follow_what_the_graph_holds_for_the_pair(store, monkeypatch):
    def owners(stake, rel_type="OWNS"):
        relationship = Relationship(
            id="e1", subject_id="o", object_id="a", rel_type=rel_type, pct=stake,
            basis="shareholding" if rel_type == "OWNS" else "directorship", is_ultimate=False,
            provenance=queries.get_entity_by_id(store, ACME).provenance, supporting_records=[],
        )  # fmt: skip
        owner = queries.get_entity_by_id(store, ACME).model_copy(update={"id": "o"})
        return lambda _store, _id: [OwnerEdge(owner=owner, relationship=relationship)]

    edge = run.ProposedEdge.model_validate(holding("O", "30", "O holds 30 %"))
    sure = [Suggestion(entity_id="o", confidence=1.0, match_type="NAME_EXACT")]
    asset = [Suggestion(entity_id="a", confidence=1.0, match_type="NAME_EXACT")]
    for stake, label in (
        (None, "FILLS_NULL_PCT"),
        (Decimal("30"), "CONFIRMS"),
        (Decimal("10"), "CONFLICTS"),
    ):
        monkeypatch.setattr(queries, "get_direct_owners", owners(stake))
        assert run.label_for(store, edge, sure, asset)[0] == label
    weak = [Suggestion(entity_id="o", confidence=0.8, match_type="NAME_FUZZY")]
    assert run.label_for(store, edge, weak, asset) == ("NEW", None)
    assert (
        run.label_for(store, edge.model_copy(update={"holding": "indirect"}), sure, asset)[0]
        == "INDIRECT_INFO"
    )
    assert (
        run.label_for(store, edge.model_copy(update={"interest": "voting"}), sure, asset)[0]
        == "INTEREST_INFO"
    )


def test_windows_cover_the_pages_in_order_up_to_the_budget():
    assert llm_extract.windows(9, 40) == ([[1, 2, 3, 4], [5, 6, 7, 8], [9]], [])
    assert llm_extract.windows(9, 5) == ([[1, 2, 3, 4], [5]], [6, 7, 8, 9])
