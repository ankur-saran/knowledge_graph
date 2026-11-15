"""Small PDFs with a text layer, written byte for byte, for the Step 15 tests and the
extraction evaluation. No PDF library writes them, so their bytes do not depend on one.

`uv run python tests/documents.py` writes the PDFs of `eval/extraction/`.
"""

from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent.parent / "eval" / "extraction"


def _escape(line: str) -> str:
    return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def pdf_bytes(pages: list[list[str]]) -> bytes:
    """A PDF of the pages, each a list of lines in Helvetica. An empty page has no text."""
    count = len(pages)
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids ["
        + b" ".join(f"{3 + 2 * i} 0 R".encode() for i in range(count))
        + f"] /Count {count} >>".encode(),
    ]
    font = 3 + 2 * count
    for i, lines in enumerate(pages):
        body = (
            "BT /F1 11 Tf 14 TL 72 740 Td "
            + " ".join(f"({_escape(line)}) Tj T*" for line in lines)
            + " ET"
            if lines
            else ""
        )
        stream = body.encode("latin-1")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R"
            f" /Resources << /Font << /F1 {font} 0 R >> >> >>".encode()
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


# The two PDFs of the evaluation set: a register as a table, and a filing in sentences.
EVAL_PDFS = {
    "doc-10-register.pdf": [
        [
            "Register of shareholders",
            "Vostrek Maritime L.L.C.",
            "Shareholder | Class | Holding",
            "Halcyon Ridge Holdings Ltd | Ordinary | 70 %",
            "Quillon Harbour Group Ltd | Ordinary | 30 %",
        ]
    ],
    "doc-11-filing.pdf": [
        ["Annual filing 2026 of Acme Trading FZE.", "Page one holds no holding."],
        [
            "Acme Trading FZE ownership.",
            "Quillon Harbour Group Ltd holds 60 % of the issued share capital",
            "of Acme Trading FZE.",
        ],
    ],
}


def write_eval_pdfs() -> None:
    for name, pages in EVAL_PDFS.items():
        (EVAL_DIR / name).write_bytes(pdf_bytes(pages))


if __name__ == "__main__":
    write_eval_pdfs()
