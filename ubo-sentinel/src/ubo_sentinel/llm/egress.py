"""What may leave the machine: the analyst's view of a decision, and nothing more.

Whoever asks, a model reads the decision as an analyst is shown it: a person is
a pseudonym, their name and identifiers masked. One annotation is then safe to
show to every role. `check_egress()` refuses a document that still holds a
value the analyst may not see, before anything is sent.
"""

import re
import unicodedata
from typing import Any

from ubo_sentinel.graph.views import mask_decision, masked_values, pseudonym_for
from ubo_sentinel.models.canonical import canonical_json
from ubo_sentinel.models.decision import Decision
from ubo_sentinel.models.memo import Memo
from ubo_sentinel.models.ontology import OntologyConfig

EGRESS_ROLE = "analyst"
EGRESS = "analyst_masked"
# A string longer than this is cut: a name or an alias needs no more, and a
# long one is more room for text written to steer a model.
MAX_TEXT = 200

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class EgressRefused(ValueError):
    """The document would send a value the analyst may not see."""


def clean(value: Any) -> Any:
    """Strings without control characters, normalised and cut to `MAX_TEXT`."""
    if isinstance(value, str):
        text = _CONTROL.sub(" ", unicodedata.normalize("NFC", value))
        return text[:MAX_TEXT]
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    return value


def egress_id(entity_id: str, entity_type: str, ontology: OntologyConfig) -> str:
    """The id a model knows an entity by: the analyst's (a person's pseudonym)."""
    hidden = ontology.roles[EGRESS_ROLE].masked_fields
    return pseudonym_for(entity_id, entity_type) if entity_type in hidden else entity_id


def egress_memo(decision: Decision, ontology: OntologyConfig) -> Memo:
    """The decision's memo as the analyst sees it."""
    return mask_decision(decision, EGRESS_ROLE, ontology).memo


def clean_page(text: str) -> str:
    """A document page as it may be sent: control characters out, line breaks kept, no cut."""
    text = unicodedata.normalize("NFC", text)
    return re.sub(r"[\x00-\x09\x0b-\x1f\x7f]", " ", text)


# --- documents (Step 15) --------------------------------------------------------------------

DOCUMENT_EGRESS = "document_consented"
# A name shorter than this is too common a word to replace.
MIN_PERSON_NAME = 4


class DocumentEgress:
    """A document's pages as a model may read them, and the way back.

    A document is the one model input that may name a person the set has never met
    (an analyst's own file), so it leaves only with the analyst's consent for that
    document. Every name and alias of every person the set does know is replaced
    first, as a whole word, by a token made of the analyst's pseudonym; `restore()`
    puts the words back in what the model wrote, so a quote is checked against the
    document as it is.
    """

    def __init__(self, pages: list[str], persons: dict[str, list[str]], consent: bool) -> None:
        if not consent:
            raise EgressRefused("a document leaves only with --allow-egress for it")
        self._tokens: dict[str, str] = {}
        surfaces: dict[str, str] = {}
        for entity_id in sorted(persons):
            for name in persons[entity_id]:
                if len(name.strip()) >= MIN_PERSON_NAME:
                    surfaces.setdefault(name.strip(), pseudonym_for(entity_id, "Person"))
        # Longest first, so a full name is replaced before a part of it.
        ordered = sorted(surfaces, key=lambda name: (-len(name), name))
        self.pages = []
        for page in pages:
            text = clean_page(page)
            for name in ordered:
                pattern = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", re.IGNORECASE)
                for written in sorted(set(pattern.findall(text))):
                    token = self._token_for(surfaces[name], written)
                    text = re.sub(rf"(?<!\w){re.escape(written)}(?!\w)", token, text)
            self.pages.append(text)

    def _token_for(self, pseudonym: str, written: str) -> str:
        for token, surface in self._tokens.items():
            if surface == written:
                return token
        token = f"[{pseudonym}~{sum(t.startswith(f'[{pseudonym}~') for t in self._tokens) + 1}]"
        self._tokens[token] = written
        return token

    @property
    def replaced(self) -> int:
        return len(self._tokens)

    def restore(self, text: str) -> str:
        for token, written in self._tokens.items():
            text = text.replace(token, written)
        return text


def check_egress(data: dict[str, Any], decision: Decision, ontology: OntologyConfig) -> None:
    text = canonical_json(data).decode("utf-8")
    leaked = [value for value in masked_values(decision, EGRESS_ROLE, ontology) if value in text]
    if leaked:
        # The kind of fault, never the value.
        raise EgressRefused("the model input holds a value the analyst may not see")
