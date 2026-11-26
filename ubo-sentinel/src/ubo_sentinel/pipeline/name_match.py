"""Name normalisation and similarity.

The one scorer for cross-source entity linking and query-time resolution, so a
name cannot match one way in Silver and another way at intake.

A name is transliterated to Latin (`anyascii`) before it is folded, so a name
written in Cyrillic, Greek or Han meets its romanisation (Step 19). The score is
the largest of three readings of the two names (word order aside, spaces
removed, particles joined), then one rule per entity type: a person's initials
and given-name variants; a company's name with a generic word added (the review
band, never a block); and two names that differ only in short tokens (acronyms
one letter apart), held below the link floor.
"""

import re
import unicodedata

from anyascii import anyascii
from rapidfuzz import fuzz

from ubo_sentinel.pipeline.name_variants import GIVEN_NAME_FORMS

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
        # Short forms in other scripts, and two that are not words anywhere. Each is
        # kept in its script and as `anyascii` writes it, since names are read after
        # transliteration: Kazakh "ТОО" is "too", and so is the English word.
        "berhad", "pvt", "ао", "зао", "оао", "ооо", "пао", "тов", "тоо", "ذمم", "شذمم",
        "too", "dhmm", "shdhmm",
    }
)  # fmt: skip

# Legal forms of several words, as runs of tokens after folding (`fold`, then
# punctuation as spaces). Left in, the words a form shares make two unrelated
# companies of one country look alike: every Russian LLC shares "obshchestvo s
# ogranichennoy otvetstvennostyu". Transliterations differ in their endings, so
# endings are open (`\w*`). Measured on the first real refresh (10.7). Names
# are transliterated before phrases are matched, so the Latin entries serve
# every script; the Cyrillic ones stay for a name read without transliteration.
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
    r"zhauapkershiligi shekteuli seriktestigi",
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


def transliterate(name: str) -> str:
    """A name in Latin letters (`anyascii`): "Газпром" -> "Gazprom", "北京" -> "BeiJing".

    Run before diacritics are folded: folded first, "й" would be "и" and lose
    the "y" every romanisation writes ("Дмитрий" is "Dmitriy").
    """
    return anyascii(unicodedata.normalize("NFKC", name))


def _tokens(name: str) -> list[str]:
    return _SEPARATORS.sub(" ", _DROPPED.sub("", fold(transliterate(name)))).split()


def normalise_name(name: str) -> str:
    """Transliterate, fold case and diacritics, strip punctuation and drop legal forms.

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


# --- Step 19: forms and rules -------------------------------------------------------------

# Words joined to the word after them in the second form: "Al-Rashid", "Al
# Rashid" and "Alrashid" are one name.
PARTICLES = frozenset({"al", "el", "bin", "ibn", "abu", "van", "von", "de", "da"})

# Words that say what a company does or how it is organised, not which one it
# is. A name that is another name with one of these added is likely its parent,
# subsidiary or trading name: worth a review, never a block.
GENERIC_TOKENS = frozenset(
    {
        "bank", "capital", "chemicals", "co", "commercial", "company", "corp", "corporation",
        "energy", "enterprises", "export", "finance", "financial", "gas", "global", "group",
        "holding", "holdings", "import", "industrial", "industries", "international",
        "investment", "investments", "logistics", "marine", "maritime", "metals", "mining",
        "oil", "partners", "petroleum", "resources", "services", "shipping", "systems",
        "technologies", "technology", "trade", "trading", "ventures",
    }
)  # fmt: skip
GENERIC_SUFFIX_SCORE = 0.90

# Two names whose only different tokens are this short on both sides are two
# acronyms, not one name misspelt ("ABC Trading", "ABD Trading"). Their score is
# held below the link floor (`LINK_FLOOR`, 0.85), so they are never linked, and
# above the resolver's floor (0.75), so a person searching still sees the other.
SHORT_TOKEN_LENGTH = 3
SHORT_TOKEN_SCORE = 0.80

# The rules switched on (19.2): each was measured alone before it was (19.x).
RULE_SPACING = "spacing"
RULE_PARTICLES = "particles"
RULE_INITIALS = "initials"
RULE_GIVEN_NAMES = "given_names"
RULE_GENERIC_SUFFIX = "generic_suffix"
RULE_SHORT_TOKENS = "short_tokens"
ALL_RULES = frozenset(
    {
        RULE_SPACING,
        RULE_PARTICLES,
        RULE_INITIALS,
        RULE_GIVEN_NAMES,
        RULE_GENERIC_SUFFIX,
        RULE_SHORT_TOKENS,
    }
)
ENABLED_RULES: frozenset[str] = ALL_RULES


def join_particles(tokens: list[str]) -> list[str]:
    """Each run of particles joined to the word after it: `al rashid` -> `alrashid`.

    A particle with no word after it stays as it is.
    """
    joined: list[str] = []
    held = ""
    for token in tokens:
        if token in PARTICLES:
            held += token
        else:
            joined.append(held + token)
            held = ""
    if held:
        joined.append(held)
    return joined


def block_tokens(name_norm: str) -> list[str]:
    """The tokens a normalised name is blocked under: its own and those of its
    particle-joined form, so "Al Rashid" and "Alrashid" share a block."""
    tokens = name_norm.split()
    if RULE_PARTICLES not in ENABLED_RULES:
        return sorted(set(tokens))
    return sorted({*tokens, *join_particles(tokens)})


def _person_forms(a: list[str], b: list[str]) -> tuple[list[str], list[str]]:
    """A person's two names with given-name variants in one form, and each initial
    replaced by the other name's word that it begins ("v putin" ~ "vladimir putin")."""
    if RULE_GIVEN_NAMES in ENABLED_RULES:
        a = [GIVEN_NAME_FORMS.get(token, token) for token in a]
        b = [GIVEN_NAME_FORMS.get(token, token) for token in b]
    if RULE_INITIALS in ENABLED_RULES:
        a, b = _expand_initials(a, b), _expand_initials(b, a)
    return a, b


def _expand_initials(tokens: list[str], other: list[str]) -> list[str]:
    free = [token for token in other if len(token) > 1 and token not in tokens]
    expanded = []
    for token in tokens:
        if len(token) == 1:
            found = next((word for word in free if word.startswith(token)), None)
            if found is not None:
                free.remove(found)
                token = found
        expanded.append(token)
    return expanded


def _generic_suffix(a: list[str], b: list[str]) -> bool:
    """One name is the other with generic words added: the same core (the
    non-generic words, spaces aside) and one name's generic words among the other's."""
    if a == b:
        return False
    core_a = "".join(token for token in a if token not in GENERIC_TOKENS)
    core_b = "".join(token for token in b if token not in GENERIC_TOKENS)
    if not core_a or core_a != core_b:
        return False
    generic_a = {token for token in a if token in GENERIC_TOKENS}
    generic_b = {token for token in b if token in GENERIC_TOKENS}
    return generic_a != generic_b and (generic_a <= generic_b or generic_b <= generic_a)


def _short_tokens_differ(a: list[str], b: list[str]) -> bool:
    only_a, only_b = set(a) - set(b), set(b) - set(a)
    return bool(only_a and only_b) and all(
        len(token) <= SHORT_TOKEN_LENGTH for token in only_a | only_b
    )


def _ratio(a: list[str], b: list[str]) -> float:
    return fuzz.token_sort_ratio(" ".join(a), " ".join(b)) / 100


def similarity_normalised(a_norm: str, b_norm: str, entity_type: str | None = None) -> float:
    """Similarity of two names that `normalise_name` has already normalised.

    The largest of: the names with word order aside (the score before Step 19),
    with spaces removed, and with particles joined. `entity_type`, when given, is
    the type of the entities the names are of, and adds its rule: initials and
    given-name variants for a `Person`; a generic word added for a `LegalEntity`.
    Names that differ only in short tokens are held below the link floor.
    """
    if a_norm == b_norm:
        return 1.0
    a, b = a_norm.split(), b_norm.split()
    if entity_type == "Person":
        a, b = _person_forms(a, b)
    score = _ratio(a, b)
    if RULE_SPACING in ENABLED_RULES:
        score = max(score, fuzz.ratio("".join(a), "".join(b)) / 100)
    if RULE_PARTICLES in ENABLED_RULES:
        score = max(score, _ratio(join_particles(a), join_particles(b)))
    if (
        entity_type == "LegalEntity"
        and RULE_GENERIC_SUFFIX in ENABLED_RULES
        and _generic_suffix(a, b)
    ):
        score = GENERIC_SUFFIX_SCORE
    if RULE_SHORT_TOKENS in ENABLED_RULES and _short_tokens_differ(a, b):
        score = min(score, SHORT_TOKEN_SCORE)
    return score


def name_similarity(a: str, b: str, entity_type: str | None = None) -> float:
    """Similarity of two names in [0, 1], independent of word order and script.

    `entity_type` is the type of the entities both names are of, when it is known.
    """
    return similarity_normalised(normalise_name(a), normalise_name(b), entity_type)
