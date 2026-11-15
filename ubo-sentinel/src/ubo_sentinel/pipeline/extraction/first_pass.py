"""Holdings read from a document by pattern, with no model and no key.

Shareholder tables and plain sentences follow a few forms; this reads those and
nothing else. What it proposes is held to the same guardrail as a model's, and
the model is measured by what it adds over this (`ubo eval --suite extraction`).
"""

import re
from decimal import Decimal

from ubo_sentinel.models.extraction import Holding, Interest, ProposedEdge
from ubo_sentinel.pipeline.extraction.document import on_page

# A name may hold dots ("L.L.C."), but a dot before a capital or the end ends a sentence.
_DOT = r"\.(?!\s+[A-Z(]|\s*$)"
_NAME = rf"[A-Z](?:[^.;:|\n]|{_DOT})*?"
_REST = rf"(?:[^.;]|{_DOT})+"
_PCT = r"(?P<pct>\d{1,3}(?:\.\d{1,6})?)\s*(?:%|per\s?cent|percent)"
# "<owner> holds 51 % of <asset>", with what lies between kept to read the kind of holding.
_HOLDS = re.compile(
    rf"(?P<owner>{_NAME})\s+(?:holds|owns|held|owned)\s+(?P<middle>[^;%]*?){_PCT}"
    rf"\s+(?:of|in)\s+(?P<tail>{_REST})"
)
_DIRECTOR = re.compile(
    rf"(?P<owner>{_NAME})\s+is\s+(?:a\s+|the\s+)?(?:sole\s+)?director\s+of\s+(?P<asset>{_REST})"
)
_TAIL_PREFIX = re.compile(
    r"^(?:the\s+)?(?:issued\s+)?(?:ordinary\s+)?(?P<kind>share capital|shares|capital|voting rights"
    r"|votes|economic interest|economic rights)?\s*(?:of|in)?\s*(?:the\s+)?",
    re.I,
)
_THROUGH = re.compile(r"\s+(?:through|via)\s+.*$", re.I)
_TABLE_ROW = re.compile(rf"^(?P<owner>[A-Za-z][^|\t%]*?)\s+{_PCT}\s*$")
_TABLE_HEADING = re.compile(r"\bshareholders?\b|\bregister of members\b", re.I)
_NOT_A_HOLDER = re.compile(r"^(?:total|name|shareholder|holder|class)\b", re.I)


def _interest(text: str) -> Interest:
    lowered = text.lower()
    if "voting" in lowered or "votes" in lowered:
        return "voting"
    if "economic" in lowered:
        return "economic"
    return "equity"


def _holding(text: str) -> Holding:
    lowered = text.lower()
    return "indirect" if "indirect" in lowered or " through " in lowered else "direct"


def _one_line(text: str) -> str:
    return " ".join(text.split()).strip(" ,")


def _sentences(page: str, number: int) -> list[ProposedEdge]:
    found: list[ProposedEdge] = []
    for match in _HOLDS.finditer(page):
        tail = match.group("tail")
        through = _THROUGH.search(tail)
        prefix = _TAIL_PREFIX.match(tail)
        kind = (prefix.group("kind") if prefix else None) or ""
        asset = _one_line(_THROUGH.sub("", _one_line(tail[prefix.end() if prefix else 0 :])))
        context = f"{match.group('middle')} {kind} {through.group(0) if through else ''}"
        if not asset:
            continue
        found.append(
            ProposedEdge(
                owner_name=_one_line(match.group("owner")),
                asset_name=asset,
                rel_type="OWNS",
                basis="shareholding",
                pct=Decimal(match.group("pct")),
                holding=_holding(context),
                interest=_interest(context),
                quote=match.group(0),
                page=number,
            )
        )
    for match in _DIRECTOR.finditer(page):
        found.append(
            ProposedEdge(
                owner_name=_one_line(match.group("owner")),
                asset_name=_one_line(match.group("asset")),
                rel_type="CONTROLS",
                basis="directorship",
                quote=match.group(0),
                page=number,
            )
        )
    return found


def _table(page: str, number: int, subject_names: list[str]) -> list[ProposedEdge]:
    """Rows of a shareholder table: the holder and the stake in the subject, named as the
    page names it (the legal name or an alias; the longest that is on the page)."""
    if not _TABLE_HEADING.search(page):
        return []
    named = [name for name in subject_names if on_page([page], 1, name)]
    subject_name = max(named, key=len) if named else subject_names[0]
    found: list[ProposedEdge] = []
    for line in page.split("\n"):
        cells = [cell.strip() for cell in line.split("|")] if "|" in line else None
        share_class = None
        if cells and len(cells) >= 2:
            row = _TABLE_ROW.match(f"{cells[0]} {cells[-1]}")
            share_class = cells[1] if len(cells) >= 3 and cells[1] else None
        else:
            row = _TABLE_ROW.match(line)
        if row is None or _NOT_A_HOLDER.match(row.group("owner")):
            continue
        found.append(
            ProposedEdge(
                owner_name=row.group("owner").strip(" ,"),
                asset_name=subject_name,
                rel_type="OWNS",
                basis="shareholding",
                pct=Decimal(row.group("pct")),
                holding=_holding(line),
                interest=_interest(line),
                share_class=share_class,
                quote=line,
                page=number,
            )
        )
    return found


def first_pass(pages: list[str], subject_names: list[str]) -> list[ProposedEdge]:
    """Every holding the patterns find, page by page, in the order they are written.
    `subject_names`: the document's subject, its legal name first, then its aliases."""
    found: list[ProposedEdge] = []
    for number, page in enumerate(pages, start=1):
        found += _sentences(page, number)
        found += _table(page, number, subject_names)
    return found
