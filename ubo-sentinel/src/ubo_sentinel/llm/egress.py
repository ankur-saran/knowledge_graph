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


def check_egress(data: dict[str, Any], decision: Decision, ontology: OntologyConfig) -> None:
    text = canonical_json(data).decode("utf-8")
    leaked = [value for value in masked_values(decision, EGRESS_ROLE, ontology) if value in text]
    if leaked:
        # The kind of fault, never the value.
        raise EgressRefused("the model input holds a value the analyst may not see")
