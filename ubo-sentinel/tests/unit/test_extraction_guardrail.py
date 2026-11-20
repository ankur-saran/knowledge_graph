"""Step 15: every proposal is held to the text it quotes; each check broken on purpose."""

from decimal import Decimal

import pytest

from ubo_sentinel.models.extraction import ProposedEdge
from ubo_sentinel.pipeline.extraction import guardrail
from ubo_sentinel.pipeline.extraction.document import normalise_text

PAGES = [
    normalise_text("Cover page."),
    normalise_text(
        "Quillon Harbour Group Ltd holds 60 % of the issued shares of Acme Trading FZE,"
        " and 75 % of its votes. Jane Doe is a director of Acme Trading FZE."
    ),
]
GOOD = ProposedEdge(
    owner_name="Quillon Harbour Group Ltd",
    asset_name="Acme Trading FZE",
    rel_type="OWNS",
    basis="shareholding",
    pct=Decimal("60"),
    quote="Quillon Harbour Group Ltd holds 60 % of the issued shares of Acme Trading FZE",
    page=2,
)
DIRECTOR = ProposedEdge(
    owner_name="Jane Doe",
    asset_name="Acme Trading FZE",
    rel_type="CONTROLS",
    basis="directorship",
    quote="Jane Doe is a director of Acme Trading FZE",
    page=2,
)


def broken(**changes):
    return GOOD.model_copy(update=changes)


def test_a_good_proposal_and_a_director_stand_with_their_spans():
    kept, faults = guardrail.check_edges([GOOD, DIRECTOR], PAGES)
    assert faults == [] and [edge for edge, _ in kept] == [GOOD, DIRECTOR]
    span = kept[0][1]
    assert span.page == 2 and PAGES[1][span.start : span.end] == GOOD.quote


@pytest.mark.parametrize(
    ("edge", "fault"),
    [
        (broken(page=3), guardrail.PAGE_NOT_IN_DOCUMENT),
        (broken(page=1), guardrail.QUOTE_NOT_FOUND),
        (
            broken(quote="Quillon Harbour Group Ltd holds 61 % of the issued shares"),
            guardrail.QUOTE_NOT_FOUND,
        ),
        (broken(owner_name="Vostrek Maritime"), guardrail.NAME_NOT_ON_PAGE),
        (broken(asset_name="Quillon Harbour Group Ltd"), guardrail.SAME_NAMES),
        (broken(pct=Decimal("75")), guardrail.PCT_NOT_IN_QUOTE),
        (broken(pct=None), guardrail.PCT_MISSING),
        (broken(basis="directorship"), guardrail.BASIS_NOT_ALLOWED),
        (DIRECTOR.model_copy(update={"pct": Decimal("5")}), guardrail.PCT_ON_CONTROL),
        (DIRECTOR.model_copy(update={"basis": "shareholding"}), guardrail.BASIS_NOT_ALLOWED),
    ],
)
def test_each_check_drops_what_breaks_it_and_names_only_the_kind(edge, fault):
    kept, faults = guardrail.check_edges([edge, GOOD], PAGES)
    # A good proposal survives a bad one beside it.
    assert [e for e, _ in kept] == [GOOD]
    assert faults == [fault]
    assert all("Quillon" not in f and "60" not in f for f in faults)


def test_percentages_are_figures_followed_by_a_percent_sign_or_the_words():
    text = "holds 60 % and 12.5% and 7 per cent and 8 percent, not 2026 nor fifty per cent or 150 %"
    assert guardrail.percentages(text) == [
        Decimal(v).quantize(Decimal("0.0001")) for v in ("60", "12.5", "7", "8")
    ]


def test_a_holding_of_votes_stands_as_a_holding_of_shares_does():
    """Step 16: a direct voting stake is a holding; the guardrail holds it to its quote."""
    votes = GOOD.model_copy(
        update={
            "interest": "voting",
            "pct": Decimal("75"),
            "quote": "and 75 % of its votes",
            "owner_name": "Quillon Harbour Group Ltd",
        }
    )
    span, fault = guardrail.check_edge(votes, PAGES)
    assert fault is None and span is not None
    wrong = votes.model_copy(update={"pct": Decimal("70")})
    assert guardrail.check_edge(wrong, PAGES) == (None, guardrail.PCT_NOT_IN_QUOTE)
