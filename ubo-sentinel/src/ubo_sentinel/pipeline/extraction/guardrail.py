"""Every proposal is held to the text it quotes, whoever proposed it.

The checks read only the stored text and the proposal, so a replay runs them again.
A proposal that fails is dropped and the kind of fault kept, never a value. No
proposal field is free text: the names must be on the page and the quote in it,
so a word a model adds of its own fails here.
"""

import re
from decimal import Decimal, InvalidOperation

from ubo_sentinel.models.canonical import quantize
from ubo_sentinel.models.extraction import ProposedEdge, Span
from ubo_sentinel.pipeline.extraction.document import find_quote, normalise_text, on_page

PAGE_NOT_IN_DOCUMENT = "PAGE_NOT_IN_DOCUMENT"
QUOTE_NOT_FOUND = "QUOTE_NOT_FOUND"
NAME_NOT_ON_PAGE = "NAME_NOT_ON_PAGE"
PCT_MISSING = "PCT_MISSING"
PCT_ON_CONTROL = "PCT_ON_CONTROL"
PCT_NOT_IN_QUOTE = "PCT_NOT_IN_QUOTE"
BASIS_NOT_ALLOWED = "BASIS_NOT_ALLOWED"
SAME_NAMES = "SAME_NAMES"

_BASES = {"OWNS": {"shareholding"}, "CONTROLS": {"directorship", "other"}}
# A percentage as figures followed by a percent sign or the words; never in words.
PERCENT = re.compile(r"(?<![\d.,])(\d{1,3}(?:\.\d{1,6})?)\s*(?:%|per\s?cent\b|percent\b)", re.I)


def percentages(text: str) -> list[Decimal]:
    """The percentages written in figures in the text, in order."""
    found = []
    for match in PERCENT.finditer(normalise_text(text)):
        try:
            value = Decimal(match.group(1))
        except InvalidOperation:  # pragma: no cover - the pattern holds digits only
            continue
        if 0 < value <= 100:
            found.append(quantize(value))
    return found


def check_edge(edge: ProposedEdge, pages: list[str]) -> tuple[Span | None, str | None]:
    """(where the quote is, None) for a proposal that stands; (None, the fault) otherwise."""
    if not 1 <= edge.page <= len(pages):
        return None, PAGE_NOT_IN_DOCUMENT
    if edge.basis not in _BASES[edge.rel_type]:
        return None, BASIS_NOT_ALLOWED
    span = find_quote(pages, edge.page, edge.quote)
    if span is None:
        return None, QUOTE_NOT_FOUND
    if normalise_text(edge.owner_name).casefold() == normalise_text(edge.asset_name).casefold():
        return None, SAME_NAMES
    if not (
        on_page(pages, edge.page, edge.owner_name) and on_page(pages, edge.page, edge.asset_name)
    ):
        return None, NAME_NOT_ON_PAGE
    if edge.rel_type == "CONTROLS":
        if edge.pct is not None:
            return None, PCT_ON_CONTROL
        return span, None
    if edge.pct is None:
        return None, PCT_MISSING
    # The stake must be one of the figures the quote itself writes.
    if quantize(edge.pct) not in percentages(edge.quote):
        return None, PCT_NOT_IN_QUOTE
    return span, None


def check_edges(
    edges: list[ProposedEdge], pages: list[str]
) -> tuple[list[tuple[ProposedEdge, Span]], list[str]]:
    """(the proposals that stand with their spans, the kinds of fault of those dropped)."""
    kept: list[tuple[ProposedEdge, Span]] = []
    faults: list[str] = []
    for edge in edges:
        span, fault = check_edge(edge, pages)
        if fault is not None:
            faults.append(fault)
        else:
            assert span is not None
            kept.append((edge, span))
    return kept, faults
