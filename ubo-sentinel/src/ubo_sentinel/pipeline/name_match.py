"""Name normalisation and similarity.

The one scorer for cross-source entity linking and query-time resolution, so a
name cannot match one way in Silver and another way at intake.
"""

import re
import unicodedata

from rapidfuzz import fuzz

# Tokens that say what kind of legal person something is, not which one. Short
# forms that are also common words ("ad", "se", "cv") are left out: dropping a
# word that names the company would make unrelated names look alike.
LEGAL_FORM_TOKENS = frozenset(
    {
        "ab", "ag", "aktiengesellschaft", "ao", "aps", "as", "asa", "bhd", "bv", "bvba",
        "cjsc", "co", "company", "corp", "corporation", "doo", "ead", "ehf", "eood",
        "eurl", "fzc", "fzco", "fze", "gmbh", "inc", "incorporated", "jsc", "kft", "kg",
        "kgaa", "kk", "lda", "limited", "llc", "llp", "lp", "ltd", "ltda", "mbh", "nv",
        "nyrt", "oao", "ohg", "ojsc", "ood", "ooo", "oy", "oyj", "pao", "pjsc", "plc",
        "pt", "pte", "pty", "sa", "sab", "sae", "sapi", "sarl", "sas", "sasu", "sau",
        "sca", "sdn", "slu", "snc", "spa", "sprl", "srl", "sro", "tbk", "tov", "ug",
        "zao", "zrt",
    }
)  # fmt: skip

_DROPPED = re.compile(r"[.'’]")
_SEPARATORS = re.compile(r"[^\w]+")


def fold(name: str) -> str:
    """Compatibility forms, case and diacritics folded: "Orlamünde" -> "orlamunde"."""
    folded = unicodedata.normalize("NFKC", name).casefold()
    decomposed = unicodedata.normalize("NFKD", folded)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return unicodedata.normalize("NFC", stripped)


def normalise_name(name: str) -> str:
    """Fold case and diacritics, strip punctuation and drop legal-form tokens.

    A name made only of legal-form tokens is kept as it is.
    """
    folded = fold(name)
    tokens = _SEPARATORS.sub(" ", _DROPPED.sub("", folded)).split()
    kept = [token for token in tokens if token not in LEGAL_FORM_TOKENS]
    return " ".join(kept or tokens)


def similarity_normalised(a_norm: str, b_norm: str) -> float:
    """Similarity of two names that `normalise_name` has already normalised."""
    return fuzz.token_sort_ratio(a_norm, b_norm) / 100


def name_similarity(a: str, b: str) -> float:
    """Similarity of two names in [0, 1], independent of word order."""
    return similarity_normalised(normalise_name(a), normalise_name(b))
