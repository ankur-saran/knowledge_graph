"""One form for an address, so two records of one place compare equal.

Case and diacritics are folded, punctuation becomes a space, and the common
words of a street address are written short ("Street" is "st", "Suite" is
"ste"). Numbers are kept: a house or suite number is what tells two buildings
on one street apart. The country is not part of the text; it is compared
beside it, as `normalise_jurisdiction()` gives it.
"""

import re
import unicodedata

# Word -> its short form. Applied to whole words after folding.
_SHORT = {
    "street": "st",
    "str": "st",
    "strasse": "st",
    "road": "rd",
    "avenue": "ave",
    "av": "ave",
    "boulevard": "blvd",
    "drive": "dr",
    "lane": "ln",
    "place": "pl",
    "square": "sq",
    "suite": "ste",
    "floor": "fl",
    "building": "bldg",
    "office": "ofc",
    "room": "rm",
    "number": "no",
    "nr": "no",
    "the": "",
}
# "P.O. Box", "PO Box", "Post Office Box": one form, before punctuation goes.
_PO_BOX = re.compile(r"\b(?:p\s*\.?\s*o\s*\.?|post\s+office)\s*box\b")
_PUNCTUATION = re.compile(r"[^\w]+", re.UNICODE)
# Shorter than this, after folding, is not an address two records can share.
MIN_LENGTH = 6


def normalise_address(text: str | None) -> str | None:
    """The comparable form of an address, or None when nothing usable is left."""
    if not text:
        return None
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in folded if not unicodedata.combining(c)).casefold()
    folded = _PO_BOX.sub(" pobox ", folded)
    words = _PUNCTUATION.sub(" ", folded).replace("_", " ").split()
    short = [_SHORT.get(word, word) for word in words]
    result = " ".join(word for word in short if word)
    return result if len(result.replace(" ", "")) >= MIN_LENGTH else None
