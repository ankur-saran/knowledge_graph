"""A document an analyst supplies: kept by its bytes, its text read out page by page.

The file is copied to `<UBO_VAR>/documents/<sha256>.<ext>` and its text to
`<sha256>.pages.json`, normalised once (`normalise_text`), with a digest. Every
quote and span refers to that text, so a later run, a review and a replay read
the same characters whatever PDF library is installed then.
"""

import hashlib
import json
import re
import shutil
import unicodedata
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ubo_sentinel.models.canonical import canonical_json, sha256_hex
from ubo_sentinel.models.extraction import Span

# Plain text: pages are separated by a form feed; a file without one is one page.
TEXT_SUFFIXES = (".txt",)
PDF_SUFFIX = ".pdf"
MAX_BYTES = 20_000_000
MAX_PAGES = 200

_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "“": '"', "”": '"', "„": '"'})
_DASHES = re.compile(r"[‐‑‒–—―−]")
_HYPHENATED = re.compile(r"(\w)-\n(\w)")
_SPACES = re.compile(r"[^\S\n]+")
_BLANK_LINES = re.compile(r"\n{2,}")
_WHITESPACE = re.compile(r"\s+")


class DocumentRefused(ValueError):
    """The file cannot be read as a document: its kind, size, pages or text layer."""


class DocumentText(BaseModel):
    """The text of one document, page by page, as every later step reads it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sha256: str
    file_name: str
    suffix: str
    extractor: str
    pages: list[str]

    @property
    def text_digest(self) -> str:
        return sha256_hex(self.pages)

    def page(self, number: int) -> str | None:
        return self.pages[number - 1] if 1 <= number <= len(self.pages) else None


def documents_dir(root: Path) -> Path:
    return root / "documents"


def normalise_text(text: str) -> str:
    """One form for text read from any file: NFKC, plain quotes and dashes, a word
    hyphenated across a line joined, runs of spaces as one; line breaks are kept
    (a table is read line by line), blank lines folded."""
    text = unicodedata.normalize("NFKC", text).translate(_QUOTES)
    text = _DASHES.sub("-", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = _HYPHENATED.sub(r"\1\2", text)
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    return _BLANK_LINES.sub("\n", "\n".join(lines)).strip()


def _flat(text: str) -> tuple[str, list[int]]:
    """The text with every run of whitespace as one space, and each character's offset."""
    flat: list[str] = []
    index: list[int] = []
    previous_space = False
    for offset, char in enumerate(text):
        if char.isspace():
            if previous_space or not flat:
                continue
            flat.append(" ")
            previous_space = True
        else:
            flat.append(char)
            previous_space = False
        index.append(offset)
    return "".join(flat), index


def find_quote(pages: list[str], page: int, quote: str) -> Span | None:
    """Where the quote is on its page, whitespace aside; None when it is not there."""
    if not 1 <= page <= len(pages):
        return None
    flat, index = _flat(pages[page - 1])
    wanted = _WHITESPACE.sub(" ", normalise_text(quote)).strip()
    at = flat.find(wanted) if wanted else -1
    if at < 0:
        return None
    return Span(page=page, start=index[at], end=index[at + len(wanted) - 1] + 1)


def on_page(pages: list[str], page: int, text: str) -> bool:
    """Whether the text is on the page, case and whitespace aside."""
    if not 1 <= page <= len(pages) or not text.strip():
        return False
    flat = _flat(pages[page - 1])[0].casefold()
    return _WHITESPACE.sub(" ", normalise_text(text)).strip().casefold() in flat


def _read_pdf(path: Path) -> list[str]:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:
        raise DocumentRefused("Reading a PDF needs pypdf: uv sync --extra docs.") from exc
    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise DocumentRefused(f"{path.name} is encrypted.")
        return [page.extract_text() or "" for page in reader.pages]
    except PdfReadError as exc:
        raise DocumentRefused(f"{path.name} is not a readable PDF ({type(exc).__name__}).") from exc


def _extractor(suffix: str) -> str:
    if suffix != PDF_SUFFIX:
        return "text"
    from importlib.metadata import version

    return f"pypdf {version('pypdf')}"


def read_document(path: Path) -> tuple[bytes, DocumentText]:
    """The file's bytes and its text. Raises `DocumentRefused`."""
    suffix = path.suffix.lower()
    if suffix not in (*TEXT_SUFFIXES, PDF_SUFFIX):
        raise DocumentRefused(f"{path.name}: only {', '.join((*TEXT_SUFFIXES, PDF_SUFFIX))}.")
    if not path.is_file():
        raise DocumentRefused(f"{path} is not a file.")
    data = path.read_bytes()
    if len(data) > MAX_BYTES:
        raise DocumentRefused(f"{path.name} is over {MAX_BYTES} bytes.")
    if suffix == PDF_SUFFIX:
        raw_pages = _read_pdf(path)
    else:
        try:
            raw_pages = data.decode("utf-8-sig").split("\f")
        except UnicodeDecodeError as exc:
            raise DocumentRefused(f"{path.name} is not UTF-8.") from exc
    if len(raw_pages) > MAX_PAGES:
        raise DocumentRefused(f"{path.name} has more than {MAX_PAGES} pages.")
    pages = [normalise_text(page) for page in raw_pages]
    if not any(pages):
        # A scan: OCR is not part of this tool.
        raise DocumentRefused(f"{path.name} has no text layer (NO_TEXT_LAYER).")
    sha = hashlib.sha256(data).hexdigest()
    return data, DocumentText(
        sha256=sha, file_name=path.name, suffix=suffix, extractor=_extractor(suffix), pages=pages
    )


def store_document(path: Path, root: Path) -> DocumentText:
    """Keep the file by its hash, and its text beside it. Storing it again changes nothing."""
    data, document = read_document(path)
    directory = documents_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    copy = directory / f"{document.sha256}{document.suffix}"
    if not copy.exists():
        shutil.copyfile(path, copy)
    text = directory / f"{document.sha256}.pages.json"
    if not text.exists():
        text.write_bytes(canonical_json(document.model_dump()) + b"\n")
    return document


def load_document(sha256: str, root: Path) -> DocumentText | None:
    """The stored text of a document, or None when it is not kept here."""
    path = documents_dir(root) / f"{sha256}.pages.json"
    if not path.is_file():
        return None
    return DocumentText.model_validate(json.loads(path.read_text(encoding="utf-8")))
