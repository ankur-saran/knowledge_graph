"""Step 15: a document kept by its bytes, its text read out once, quotes found in it."""

import pytest
from documents import pdf_bytes

from ubo_sentinel.models.extraction import Span
from ubo_sentinel.pipeline.extraction.document import (
    DocumentRefused,
    find_quote,
    load_document,
    normalise_text,
    on_page,
    read_document,
    store_document,
)


def test_text_is_normalised_once_and_line_breaks_are_kept():
    text = "Halcyon Ridge  Holdings “Ltd” – 51 %\nshare-\nholder\r\n\n\nend"
    assert normalise_text(text) == 'Halcyon Ridge Holdings "Ltd" - 51 %\nshareholder\nend'
    assert normalise_text(normalise_text(text)) == normalise_text(text)


def test_a_quote_is_found_whitespace_aside_and_its_span_reads_back():
    pages = [normalise_text("Intro.\nQuillon Harbour Group Ltd holds 60 % of the issued\nshares.")]
    span = find_quote(pages, 1, "Quillon Harbour Group Ltd holds 60 %  of the issued shares")
    assert span == Span(page=1, start=7, end=pages[0].index("shares") + len("shares"))
    assert pages[0][span.start : span.end].startswith("Quillon")
    assert find_quote(pages, 1, "Quillon holds 61 %") is None
    assert find_quote(pages, 2, "Intro") is None
    assert on_page(pages, 1, "quillon  harbour") and not on_page(pages, 1, "Vostrek")


def test_plain_text_pages_are_split_on_form_feeds(tmp_path):
    path = tmp_path / "two.txt"
    path.write_text("﻿First page.\fSecond page.", encoding="utf-8")
    _, document = read_document(path)
    assert document.pages == ["First page.", "Second page."]
    assert document.extractor == "text" and document.page(2) == "Second page."
    assert document.page(3) is None


def test_a_pdf_with_a_text_layer_is_read_page_by_page(tmp_path):
    path = tmp_path / "filing.pdf"
    path.write_bytes(pdf_bytes([["Page one (cover)."], ["Owner A holds 60 % of", "Target B."]]))
    _, document = read_document(path)
    assert document.pages == ["Page one (cover).", "Owner A holds 60 % of\nTarget B."]
    assert document.extractor.startswith("pypdf ")


@pytest.mark.parametrize(
    ("name", "content", "kind"),
    [
        ("scan.pdf", pdf_bytes([[], []]), "no text layer"),
        ("notes.docx", b"anything", "only"),
        ("broken.pdf", b"%PDF-1.4 not really", "not a readable PDF"),
        ("latin.txt", "Café".encode("latin-1"), "not UTF-8"),
        ("empty.txt", b"  \n ", "no text layer"),
    ],
)
def test_what_cannot_be_read_as_a_document_is_refused(tmp_path, name, content, kind):
    path = tmp_path / name
    path.write_bytes(content)
    with pytest.raises(DocumentRefused, match=kind):
        read_document(path)


def test_a_missing_or_oversized_file_is_refused(tmp_path, monkeypatch):
    with pytest.raises(DocumentRefused, match="not a file"):
        read_document(tmp_path / "gone.txt")
    big = tmp_path / "big.txt"
    big.write_text("x" * 50, encoding="utf-8")
    monkeypatch.setattr("ubo_sentinel.pipeline.extraction.document.MAX_BYTES", 10)
    with pytest.raises(DocumentRefused, match="over 10 bytes"):
        read_document(big)
    monkeypatch.setattr("ubo_sentinel.pipeline.extraction.document.MAX_PAGES", 1)
    many = tmp_path / "many.txt"
    many.write_text("a\fb", encoding="utf-8")
    with pytest.raises(DocumentRefused, match="more than 1 pages"):
        read_document(many)


def test_a_document_is_kept_by_its_hash_and_storing_it_again_changes_nothing(tmp_path):
    source = tmp_path / "in" / "register.txt"
    source.parent.mkdir()
    source.write_text("Register of shareholders.", encoding="utf-8")
    root = tmp_path / "var"
    document = store_document(source, root)
    kept = root / "documents" / f"{document.sha256}.txt"
    text = root / "documents" / f"{document.sha256}.pages.json"
    assert kept.read_bytes() == source.read_bytes()
    before = text.read_bytes()
    assert store_document(source, root) == document and text.read_bytes() == before
    assert load_document(document.sha256, root) == document
    assert load_document("0" * 64, root) is None
