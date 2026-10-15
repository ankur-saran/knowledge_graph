import pytest

from ubo_sentinel.pipeline.name_match import name_similarity, normalise_name


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Acme Trading FZE", "acme trading"),
        ("VOSTREK MARITIME, L.L.C.", "vostrek maritime"),
        ("Pellucid Optics (Pte.) Ltd.", "pellucid optics"),
        ("Yusuf Al-Qahdari", "yusuf al qahdari"),
        # Diacritics are folded, in every script: й is и with a breve.
        ("Дмитрий Волканов", "дмитрии волканов"),
        ("Orlamünde Präzision GmbH", "orlamunde prazision"),
        ("Société Générale S.A.", "societe generale"),
        ("Straße", "strasse"),
        ("ＡＣＭＥ　Trading", "acme trading"),
        # Legal forms of more jurisdictions.
        ("PT Bank Mandiri Tbk", "bank mandiri"),
        ("Kovács Kft.", "kovacs"),
        ("Novak d.o.o.", "novak"),
        ("Vaskor Oyj", "vaskor"),
        # Nothing but legal-form tokens: kept, so the name is never empty.
        ("Company Ltd", "company ltd"),
    ],
)
def test_normalise_name(name, expected):
    assert normalise_name(name) == expected


def test_diacritics_do_not_lower_similarity():
    assert name_similarity("Orlamünde Präzision", "Orlamunde Prazision GmbH") == 1.0


def test_similarity_ignores_case_legal_form_and_word_order():
    assert name_similarity("Acme Trading FZE", "acme trading llc") == 1.0
    assert name_similarity("Thornaby Rolling Mills Ltd", "Mills Thornaby Rolling") == 1.0


def test_similarity_orders_a_typo_above_an_unrelated_name():
    typo = name_similarity("Acme Trading FZE", "Acme Tradng FZE")
    unrelated = name_similarity("Acme Trading FZE", "Quillon Harbour Group Ltd")
    assert 0 <= unrelated < 0.75 < typo < 1
