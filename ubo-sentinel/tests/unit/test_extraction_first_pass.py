"""Step 15: the parser reads a few forms of holding, and nothing it cannot quote."""

from decimal import Decimal

from ubo_sentinel.pipeline.extraction.document import normalise_text
from ubo_sentinel.pipeline.extraction.first_pass import first_pass
from ubo_sentinel.pipeline.extraction.guardrail import check_edges


def read(text, subject="Acme Trading FZE"):
    pages = [normalise_text(page) for page in text.split("\f")]
    return first_pass(pages, [subject]), pages


def test_a_sentence_names_owner_asset_stake_and_kind():
    edges, pages = read(
        "Vostrek Maritime L.L.C. holds 55 % of the issued shares of Acme Trading FZE. "
        "Tobias Wrenfield holds an indirect 24 per cent of Acme Trading FZE through Quillon"
        " Harbour Group Ltd. Mira Vale holds 12.5% of the voting rights in Acme Trading FZE."
    )
    got = [(e.owner_name, e.asset_name, e.pct, e.holding, e.interest) for e in edges]
    assert got == [
        ("Vostrek Maritime L.L.C.", "Acme Trading FZE", Decimal("55"), "direct", "equity"),
        ("Tobias Wrenfield", "Acme Trading FZE", Decimal("24"), "indirect", "equity"),
        ("Mira Vale", "Acme Trading FZE", Decimal("12.5"), "direct", "voting"),
    ]
    assert check_edges(edges, pages)[1] == []


def test_a_sentence_that_wraps_across_lines_is_read_and_quoted():
    edges, pages = read(
        "Quillon Harbour Group Ltd holds 60 % of the issued share capital\nof Acme Trading FZE."
    )
    assert [(e.owner_name, e.asset_name) for e in edges] == [
        ("Quillon Harbour Group Ltd", "Acme Trading FZE")
    ]
    assert check_edges(edges, pages)[1] == []


def test_a_director_is_a_control_without_a_stake():
    (edge,), _ = read("Board.\nJane Doe is a director of Acme Trading FZE.")
    assert (edge.owner_name, edge.rel_type, edge.basis, edge.pct) == (
        "Jane Doe",
        "CONTROLS",
        "directorship",
        None,
    )


def test_a_table_needs_a_heading_and_names_the_subject_as_the_page_does():
    table = "Shareholder | Class | Holding\nHalcyon Ridge Holdings Ltd | Ordinary | 70 %\nTotal | | 100 %"
    edges, pages = read(
        f"Register of shareholders\nVostrek Maritime L.L.C.\n{table}", "Vostrek Maritime LLC"
    )
    # The subject's legal name is not on the page: the row is proposed, and dropped...
    assert check_edges(edges, pages) == ([], ["NAME_NOT_ON_PAGE"])
    edges = first_pass(pages, ["Vostrek Maritime LLC", "Vostrek Maritime L.L.C."])
    (edge,) = edges  # ...its alias is; "Total" is not a holder
    assert (edge.owner_name, edge.asset_name, edge.pct, edge.share_class) == (
        "Halcyon Ridge Holdings Ltd",
        "Vostrek Maritime L.L.C.",
        Decimal("70"),
        "Ordinary",
    )
    assert read("Owners\nHalcyon Ridge Holdings Ltd | 70 %")[0] == []


def test_a_stake_in_words_or_a_passive_sentence_is_not_read():
    assert read("Quillon Harbour Group Ltd holds fifty-one per cent of Acme Trading FZE.")[0] == []
    assert (
        read("60 % of the shares of Acme Trading FZE are held by Quillon Harbour Group Ltd.")[0]
        == []
    )
