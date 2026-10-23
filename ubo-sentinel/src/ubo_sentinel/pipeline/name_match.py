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
        # Short forms in other scripts, and two that are not words anywhere.
        "berhad", "pvt", "ао", "зао", "оао", "ооо", "пао", "тов", "тоо", "ذمم", "شذمم",
    }
)  # fmt: skip

# Legal forms of several words, as runs of tokens after folding (`fold`, then
# punctuation as spaces). Left in, the words a form shares make two unrelated
# companies of one country look alike: every Russian LLC shares "obshchestvo s
# ogranichennoy otvetstvennostyu". Transliterations differ in their endings, so
# endings are open (`\w*`). Measured on the first real refresh (10.7).
LEGAL_FORM_PHRASES = (
    # Russian: limited liability company, joint-stock company (in Cyrillic and Latin).
    r"общество с ограниченн\w* ответственност\w*",
    r"(?:непубличное |публичное |закрытое |открытое )?акционерное общество",
    r"obs\w*estvo s ogranichenn\w* otvetstvenn\w*",
    r"(?:nepublichnoe |publichnoe |zakrytoe |otkrytoe )?aktsionernoe obs\w*estvo",
    # Ukrainian and Kazakh.
    r"товариство з обмежен\w* відповідальн\w*",
    r"(?:приватне |публічне )?акціонерне товариство",
    r"tovar\w*stvo z obmez\w* vidpovid\w*",
    r"(?:pryvatne |publichne )?aktsionerne tovar\w*stvo",
    r"жауапкершілігі шектеулі серіктестігі",
    # Polish, Czech and Slovak, Turkish.
    r"spo[łl]ka z ograniczon\w* odpowiedzialnosci\w*",
    r"spo[łl]ka (?:komandytowa|akcyjna|jawna)",
    r"sp z oo",
    r"spol s r o",
    r"(?:anonim|limited) sirketi",
    # English.
    r"(?:public |private )?limited liability (?:company|partnership)",
    r"(?:public |private |closed |open )?joint stock company",
    r"(?:public|private) limited company",
    r"private limited",
    r"limited partnership",
    # German, French, Spanish, Portuguese, Italian, Dutch, Japanese, Malay.
    r"gesellschaft mit beschrankter haftung",
    r"societe (?:anonyme|a responsabilite limitee|par actions simplifiee)",
    r"sociedad (?:anonima|de responsabilidad limitada)",
    r"sociedade (?:anonima|limitada)",
    r"societa (?:per azioni|a responsabilita limitata)",
    r"(?:besloten|naamloze) vennootschap",
    r"(?:kabushiki|godo|yugen) kaisha",
    r"sendirian berhad",
)
_PHRASE = re.compile(r"(?<!\S)(?:" + "|".join(LEGAL_FORM_PHRASES) + r")(?!\S)")

# A legal form written out and its short form, for comparing names with their
# forms kept (`form_similarity`): "Siemens Aktiengesellschaft" is "siemens ag".
_SHORT_FORMS = {
    "aktiengesellschaft": "ag",
    "company": "co",
    "corporation": "corp",
    "incorporated": "inc",
    "limited": "ltd",
}

_DROPPED = re.compile(r"[.'’]")
_SEPARATORS = re.compile(r"[^\w]+")


def fold(name: str) -> str:
    """Compatibility forms, case and diacritics folded: "Orlamünde" -> "orlamunde"."""
    folded = unicodedata.normalize("NFKC", name).casefold()
    decomposed = unicodedata.normalize("NFKD", folded)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return unicodedata.normalize("NFC", stripped)


def _tokens(name: str) -> list[str]:
    return _SEPARATORS.sub(" ", _DROPPED.sub("", fold(name))).split()


def normalise_name(name: str) -> str:
    """Fold case and diacritics, strip punctuation and drop legal forms.

    Forms of several words go first (`LEGAL_FORM_PHRASES`), then single tokens.
    A name made only of legal forms is kept as it is.
    """
    tokens = _tokens(name)
    unphrased = _PHRASE.sub(" ", " ".join(tokens)).split()
    kept = [token for token in unphrased if token not in LEGAL_FORM_TOKENS]
    return " ".join(kept or tokens)


def form_key(name: str) -> str:
    """A name folded with its legal forms kept, each in its short form."""
    return " ".join(_SHORT_FORMS.get(token, token) for token in _tokens(name))


def form_similarity(a: str, b: str) -> float:
    """Similarity of two names with their legal forms kept.

    Not a match score: it orders names that `normalise_name` makes equal, so
    "Siemens Aktiengesellschaft" comes before "Siemens Limited" for "Siemens AG".
    """
    return fuzz.token_sort_ratio(form_key(a), form_key(b)) / 100


def similarity_normalised(a_norm: str, b_norm: str) -> float:
    """Similarity of two names that `normalise_name` has already normalised."""
    return fuzz.token_sort_ratio(a_norm, b_norm) / 100


def name_similarity(a: str, b: str) -> float:
    """Similarity of two names in [0, 1], independent of word order."""
    return similarity_normalised(normalise_name(a), normalise_name(b))
