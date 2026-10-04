import pytest

from ubo_sentinel.pipeline.name_match import name_similarity, normalise_name


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Acme Trading FZE", "acme trading"),
        ("VOSTREK MARITIME, L.L.C.", "vostrek maritime"),
        ("Pellucid Optics (Pte.) Ltd.", "pellucid optics"),
        ("Yusuf Al-Qahdari", "yusuf al qahdari"),
        ("Дмитрий Волканов", "дмитрий волканов"),
        # Nothing but legal-form tokens: kept, so the name is never empty.
        ("Company Ltd", "company ltd"),
    ],
)
def test_normalise_name(name, expected):
    assert normalise_name(name) == expected


def test_similarity_ignores_case_legal_form_and_word_order():
    assert name_similarity("Acme Trading FZE", "acme trading llc") == 1.0
    assert name_similarity("Thornaby Rolling Mills Ltd", "Mills Thornaby Rolling") == 1.0


def test_similarity_orders_a_typo_above_an_unrelated_name():
    typo = name_similarity("Acme Trading FZE", "Acme Tradng FZE")
    unrelated = name_similarity("Acme Trading FZE", "Quillon Harbour Group Ltd")
    assert 0 <= unrelated < 0.75 < typo < 1
