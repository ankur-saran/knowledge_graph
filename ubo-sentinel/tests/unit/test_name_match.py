import pytest

from ubo_sentinel.pipeline.name_match import form_similarity, name_similarity, normalise_name


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
        # Legal forms of several words, in their scripts and transliterations.
        ('Общество с ограниченной ответственностью "Корпус"', "корпус"),
        ("OBSHCHESTVO S OGRANICHENNOY OTVETSTVENNOSTYU ELBI INVEST", "elbi invest"),
        ("Obschestvo s ogranichennoj otvetstvennostju Profit", "profit"),
        ("Публичное акционерное общество «Новатэк»", "новатэк"),
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
        ("ООО Ромашка", "ромашка"),
        # A phrase is whole words: "private" alone, or inside a name, stays.
        ("Private Wealth Partners", "private wealth partners"),
        ("Joint Stockyards Company", "joint stockyards"),
        # A name that is only a legal form is kept whole.
        ("Общество с ограниченной ответственностью", "общество с ограниченнои ответственностью"),
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
