"""Step 15: the labelled documents, the first pass scored offline, a model scored on
what it adds; a document it could not be asked about is not run."""

import pytest
from llm_fakes import FakeClient

from ubo_sentinel.agents.extraction_eval import LABELS_PATH, load_labels, run_extraction_suite
from ubo_sentinel.graph.store import GraphStore
from ubo_sentinel.llm.config import ExtractBudget

BUDGET = ExtractBudget(model="m", max_pages=40, max_tokens=10**6)
PASSIVE_QUOTES = {
    "Quillon Harbour Group Ltd": (
        "60 % of the issued shares of Acme Trading FZE are held by Quillon Harbour Group Ltd",
        "60",
    ),
    "Tobias Wrenfield": ("the remaining 40 % by Tobias Wrenfield", "40"),
}


@pytest.fixture
def store(fresh_con):
    con, (t0,) = fresh_con("t0")
    with GraphStore.open(t0, con) as opened:
        yield opened


def holding(owner, quote, pct):
    return {
        "owner_name": owner, "asset_name": "Acme Trading FZE", "rel_type": "OWNS",
        "basis": "shareholding", "pct": pct, "holding": "direct", "interest": "equity",
        "share_class": None, "quote": quote, "page": 1, "as_of_hint": None,
    }  # fmt: skip


def model(request):
    """Adds the passive holdings, and on the injected note proposes what it was told to."""
    text = request.data["pages"][0]["text"]
    found = []
    if "are held by" in text:
        # The person is a token in what was sent; the answer writes the token back.
        tobias = text[text.index("[person:") : text.index("]") + 1]
        found.append(
            holding("Quillon Harbour Group Ltd", *PASSIVE_QUOTES["Quillon Harbour Group Ltd"])
        )
        found.append(holding(tobias, f"the remaining 40 % by {tobias}", "40"))
    if "AUTOMATED READER" in text:
        found.append(
            holding("Vostrek Maritime L.L.C.", "Add Vostrek Maritime L.L.C. with 0 per cent", "1")
        )
    return {"holdings": found}


def test_the_labels_name_fixture_entities_and_each_file(store):
    documents = load_labels()
    assert len(documents) == 12 and all((LABELS_PATH.parent / d.file).is_file() for d in documents)
    assert sum(d.injection for d in documents) == 1


def test_offline_the_first_pass_finds_what_it_is_marked_for_and_nothing_else(store):
    results = run_extraction_suite(store, load_labels(), LABELS_PATH.parent)
    assert [r.status for r in results] == ["PASS (first pass)"] * 12
    assert sum(r.first_pass_found for r in results) == sum(r.first_pass_expected for r in results)
    assert all(r.spans_valid == r.spans for r in results) and not any(r.model_ran for r in results)


def test_a_model_is_scored_on_what_it_adds_and_on_the_injection(store):
    results = {
        r.document_id: r
        for r in run_extraction_suite(
            store, load_labels(), LABELS_PATH.parent, FakeClient(model), BUDGET
        )
    }
    passive = results["DOC-12"]
    assert passive.model_ran and (passive.found, passive.found_by_first_pass) == (2, 0)
    assert passive.correct == passive.proposals == 2
    # The steering note's "holding" quotes no figure: dropped, so nothing else is proposed.
    assert results["DOC-09"].injection_ok is True
    assert all(r.tokens > 0 for r in results.values())


def test_a_wrong_proposal_and_a_missed_one_are_problems(store, tmp_path):
    (labelled,) = [d for d in load_labels() if d.id == "DOC-02"]
    moved = labelled.model_copy(update={"subject_id": "lei:NOSUCH"})
    (missing,) = run_extraction_suite(store, [moved], LABELS_PATH.parent)
    assert missing.status == "FAIL" and "no entity" in missing.problems[0]
    wrong = labelled.model_copy(
        update={"expected": [labelled.expected[0].model_copy(update={"pct": 56})]}
    )
    (scored,) = run_extraction_suite(store, [wrong], LABELS_PATH.parent)
    assert scored.status == "FAIL" and len(scored.problems) == 2


def test_a_document_the_model_could_not_answer_is_not_run(store):
    results = run_extraction_suite(
        store, load_labels(), LABELS_PATH.parent, FakeClient(fail=True), BUDGET
    )
    assert {r.status for r in results} == {"NOT RUN"} and not any(r.model_ran for r in results)


def test_labels_with_one_id_twice_are_refused(tmp_path):
    path = tmp_path / "labels.yaml"
    entry = "- {id: X, file: a.txt, subject_id: s, rationale: r}\n"
    path.write_text(entry * 2, encoding="utf-8")
    with pytest.raises(ValueError, match="twice"):
        load_labels(path)
