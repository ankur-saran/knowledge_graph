import pytest

from ubo_sentinel.pipeline import name_match
from ubo_sentinel.pipeline.entity_linking import LINK_FLOOR
from ubo_sentinel.pipeline.name_match import (
    ALL_RULES,
    GENERIC_SUFFIX_SCORE,
    block_tokens,
    form_similarity,
    join_particles,
    name_similarity,
    normalise_name,
)
from ubo_sentinel.rules.pack import load_rule_pack, pack_path

PACK = load_rule_pack(pack_path("ofac"))


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Acme Trading FZE", "acme trading"),
        ("VOSTREK MARITIME, L.L.C.", "vostrek maritime"),
        ("Pellucid Optics (Pte.) Ltd.", "pellucid optics"),
        ("Yusuf Al-Qahdari", "yusuf al qahdari"),
        # Every script is transliterated to Latin first (Step 19), then folded.
        ("Дмитрий Волканов", "dmitriy volkanov"),
        ("Газпром", "gazprom"),
        ("北京大学", "beijingdaxue"),
        ("Ελληνικά Πετρέλαια", "ellinika petrelaia"),
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
        # Legal forms of several words, in their scripts and transliterations.
        ('Общество с ограниченной ответственностью "Корпус"', "korpus"),
        ("OBSHCHESTVO S OGRANICHENNOY OTVETSTVENNOSTYU ELBI INVEST", "elbi invest"),
        ("Obschestvo s ogranichennoj otvetstvennostju Profit", "profit"),
        ("Публичное акционерное общество «Новатэк»", "novatek"),
        ("Товариство з обмеженою відповідальністю «Абі»", "abi"),
        ("ТОО «Казмунай»", "kazmunay"),
        ("TOVARYSTVO Z OBMEZHENOIU VIDPOVIDALNISTIU ABI UKRAYINA", "abi ukrayina"),
        (
            '"ABC-TRANSPORT" SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ SPÓŁKA KOMANDYTOWA',
            "abc transport",
        ),
        ("Acme Sp. z o.o.", "acme"),
        ("FONERIA PORTFÖY YÖNETİMİ ANONİM ŞİRKETİ", "foneria portfoy yonetimi"),
        ("QB Group Limited Liability Company", "qb group"),
        ("Shell Public Limited Company", "shell"),
        ("Public Joint Stock Company Novatek", "novatek"),
        ("Halvor Gesellschaft mit beschränkter Haftung", "halvor"),
        ("Toyota Jidosha Kabushiki Kaisha", "toyota jidosha"),
        ("ООО Ромашка", "romashka"),
        # A phrase is whole words: "private" alone, or inside a name, stays.
        ("Private Wealth Partners", "private wealth partners"),
        ("Joint Stockyards Company", "joint stockyards"),
        # A name that is only a legal form is kept whole.
        (
            "Общество с ограниченной ответственностью",
            "obshchestvo s ogranichennoy otvetstvennostyu",
        ),
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


def test_unrelated_companies_of_one_legal_form_do_not_look_alike():
    """Measured on the first real refresh: two Russian LLCs scored 0.77 on their form alone."""
    a = "Obshchestvo s ogranichennoy otvetstvennostyu Elbi Invest"
    b = "Obshchestvo s ogranichennoi otvetstvennostyu Progress"
    assert name_similarity(a, b) < 0.5


def test_form_similarity_orders_namesakes_by_their_legal_form():
    query = "Siemens Aktiengesellschaft"
    assert name_similarity(query, "Siemens Limited") == name_similarity(query, "Siemens AG") == 1
    assert form_similarity(query, "Siemens AG") == 1.0
    assert form_similarity(query, "Siemens Limited") < 1.0
    assert form_similarity("Microsoft Corp", "MICROSOFT CORPORATION") == 1.0


# --- Step 19 ------------------------------------------------------------------------------

# The six probe pairs of the 2026-11-19 assessment, and where each belongs: a match
# (at or above the pack's match threshold), the review band (from its lower edge to
# the threshold), or below the link floor (never linked). Before Step 19 they
# scored 0.00, 0.57, 0.76, 0.67 and 0.91 (the sixth, the plan's generic-suffix
# example, 0.82).
PROBES = [
    ("Gazprom", "Газпром", "LegalEntity", "match"),
    ("Mohammed Al-Rashid", "Muhammad Alrashid", "Person", "match"),
    ("V. Putin", "Vladimir Putin", "Person", "match"),
    ("North Star", "Northstar Shipping", "LegalEntity", "review"),
    ("Rosneft", "Rosneft Oil Company", "LegalEntity", "review"),
    ("ABC Trading", "ABD Trading", "LegalEntity", "unlinked"),
]


@pytest.mark.parametrize(("a", "b", "entity_type", "side"), PROBES)
def test_probe_pairs_score_on_their_side_of_the_band(a, b, entity_type, side):
    score = name_similarity(a, b, entity_type)
    assert score == name_similarity(b, a, entity_type)
    if side == "match":
        assert score >= PACK.match_threshold
    elif side == "review":
        assert PACK.review_band_low <= score < PACK.match_threshold
    else:
        assert score < LINK_FLOOR


def test_a_name_in_another_script_finds_its_romanisation():
    assert name_similarity("Дмитрий Волканов", "Dmitriy Volkanov") == 1.0
    assert name_similarity("北京大学", "Beijing Daxue") == 1.0


def test_particles_join_the_word_after_them():
    assert join_particles(["abu", "al", "rashid", "trading"]) == ["abualrashid", "trading"]
    assert join_particles(["rashid", "al"]) == ["rashid", "al"]
    assert set(block_tokens("al rashid")) == {"al", "rashid", "alrashid"}


def test_spaces_removed():
    assert name_similarity("North Star Maritime", "Northstar Maritime") == 1.0


def test_an_initial_matches_only_a_word_it_begins_and_only_for_a_person():
    assert name_similarity("V. Putin", "Vladimir Putin", "Person") == 1.0
    assert name_similarity("W. Putin", "Vladimir Putin", "Person") < LINK_FLOOR
    assert name_similarity("V. Putin", "Vladimir Putin", "LegalEntity") < LINK_FLOOR


def test_given_name_variants_only_for_a_person():
    assert name_similarity("Aleksandr Varensky", "Alexander Varensky", "Person") == 1.0
    assert name_similarity("Mohamed Haddad", "Mohammad Haddad", "Person") == 1.0
    # Two given names that are not variants of each other stay two people.
    assert name_similarity("Sergey Varensky", "Andrey Varensky", "Person") < LINK_FLOOR


def test_a_generic_word_added_is_the_review_band_never_a_block():
    assert name_similarity("Halvorsen", "Halvorsen Holding Group", "LegalEntity") == (
        GENERIC_SUFFIX_SCORE
    )
    # Not for a person, and not when the other words differ.
    assert name_similarity("Halvorsen", "Halvorsen Holding", "Person") < PACK.review_band_low
    assert name_similarity("Halvorsen Shipping", "Brekke Shipping", "LegalEntity") < LINK_FLOOR
    # Both names generic words only: no core to compare.
    assert name_similarity("Holding Group", "Holding", "LegalEntity") < PACK.match_threshold


def test_names_that_differ_only_in_acronyms_are_never_linked():
    assert name_similarity("ABC Trading", "ABD Trading") < LINK_FLOOR
    assert name_similarity("KTM Logistics", "KTN Logistics") < LINK_FLOOR
    # A short token against a long one is not two acronyms.
    assert name_similarity("Acme Trading", "Acmes Trading") >= LINK_FLOOR


def test_every_rule_can_be_switched_off(monkeypatch):
    """Each rule is measured alone (19.x): with none on, the score is the score before Step 19."""
    monkeypatch.setattr(name_match, "ENABLED_RULES", frozenset())
    assert name_similarity("North Star", "Northstar Shipping", "LegalEntity") < LINK_FLOOR
    assert name_similarity("V. Putin", "Vladimir Putin", "Person") == pytest.approx(0.67, abs=0.01)
    assert block_tokens("al rashid") == ["al", "rashid"]
    for rule in sorted(ALL_RULES):
        monkeypatch.setattr(name_match, "ENABLED_RULES", frozenset({rule}))
        assert 0.0 <= name_similarity("Mohammed Al-Rashid", "Muhammad Alrashid", "Person") <= 1.0
