"""One form for a jurisdiction, whatever the source wrote.

Sources disagree on the form: GLEIF gives `US-DE` or `GB`, OpenSanctions `ru`
or `cy`. Silver compares jurisdictions when it links names and the resolver
ranks by them, so every mapper writes the ISO 3166-1 alpha-2 country, upper
case. The subdivision stays in the Bronze record.
"""

import re

_COUNTRY = re.compile(r"^[A-Z]{2}$")


def normalise_jurisdiction(value: str | None) -> str | None:
    """`us-de` -> `US`, `gb` -> `GB`. None for anything that is not a country code.

    A value that cannot be read is treated as no jurisdiction, never as another one.
    """
    if value is None:
        return None
    country = value.strip().upper().split("-", 1)[0].strip()
    return country if _COUNTRY.match(country) else None
