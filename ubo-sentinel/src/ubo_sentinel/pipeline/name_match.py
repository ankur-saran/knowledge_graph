"""Name normalisation and similarity.

The one scorer for cross-source entity linking and query-time resolution, so a
name cannot match one way in Silver and another way at intake.
"""

import re
import unicodedata

from rapidfuzz import fuzz

# Tokens that say what kind of legal person something is, not which one.
LEGAL_FORM_TOKENS = frozenset(
    {
        "ab", "ag", "ao", "aps", "as", "bv", "co", "company", "corp", "corporation",
        "fzc", "fzco", "fze", "gmbh", "inc", "incorporated", "jsc", "limited", "llc",
        "llp", "lp", "ltd", "nv", "oao", "ooo", "pao", "pjsc", "plc", "pte", "pty",
        "sa", "sarl", "spa", "srl", "zao",
    }
)  # fmt: skip

_DROPPED = re.compile(r"[.'’]")
_SEPARATORS = re.compile(r"[^\w]+")


def normalise_name(name: str) -> str:
    """Case-fold, strip punctuation and drop legal-form tokens.

    A name made only of legal-form tokens is kept as it is.
    """
    folded = unicodedata.normalize("NFKC", name).casefold()
    tokens = _SEPARATORS.sub(" ", _DROPPED.sub("", folded)).split()
    kept = [token for token in tokens if token not in LEGAL_FORM_TOKENS]
    return " ".join(kept or tokens)


def similarity_normalised(a_norm: str, b_norm: str) -> float:
    """Similarity of two names that `normalise_name` has already normalised."""
    return fuzz.token_sort_ratio(a_norm, b_norm) / 100


def name_similarity(a: str, b: str) -> float:
    """Similarity of two names in [0, 1], independent of word order."""
    return similarity_normalised(normalise_name(a), normalise_name(b))
