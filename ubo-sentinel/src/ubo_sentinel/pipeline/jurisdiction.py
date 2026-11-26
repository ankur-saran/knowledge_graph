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


# A nationality as Companies House writes it (free text, a demonym) -> the country
# (Step 19). Only the forms that name one country without doubt; any other text is
# no nationality, never a guess. The UK's nations are the UK.
NATIONALITY_WORDS = {
    "afghan": "AF", "albanian": "AL", "algerian": "DZ", "american": "US", "argentine": "AR",
    "argentinian": "AR", "armenian": "AM", "australian": "AU", "austrian": "AT",
    "azerbaijani": "AZ", "bahraini": "BH", "bangladeshi": "BD", "belarusian": "BY",
    "belgian": "BE", "bosnian": "BA", "brazilian": "BR", "british": "GB", "bulgarian": "BG",
    "canadian": "CA", "chilean": "CL", "chinese": "CN", "colombian": "CO", "croatian": "HR",
    "cypriot": "CY", "czech": "CZ", "danish": "DK", "dutch": "NL", "egyptian": "EG",
    "emirati": "AE", "english": "GB", "estonian": "EE", "filipino": "PH", "finnish": "FI",
    "french": "FR", "georgian": "GE", "german": "DE", "ghanaian": "GH", "greek": "GR",
    "hungarian": "HU", "icelandic": "IS", "indian": "IN", "indonesian": "ID", "iranian": "IR",
    "iraqi": "IQ", "irish": "IE", "israeli": "IL", "italian": "IT", "japanese": "JP",
    "jordanian": "JO", "kazakh": "KZ", "kazakhstani": "KZ", "kenyan": "KE", "korean": "KR",
    "south korean": "KR", "kuwaiti": "KW", "kyrgyz": "KG", "latvian": "LV", "lebanese": "LB",
    "libyan": "LY", "lithuanian": "LT", "luxembourgish": "LU", "malaysian": "MY",
    "maltese": "MT", "mexican": "MX", "moldovan": "MD", "moroccan": "MA", "new zealander": "NZ",
    "nigerian": "NG", "north korean": "KP", "northern irish": "GB", "norwegian": "NO",
    "omani": "OM", "pakistani": "PK", "polish": "PL", "portuguese": "PT", "qatari": "QA",
    "romanian": "RO", "russian": "RU", "saudi": "SA", "saudi arabian": "SA", "scottish": "GB",
    "serbian": "RS", "singaporean": "SG", "slovak": "SK", "slovenian": "SI",
    "south african": "ZA", "spanish": "ES", "sri lankan": "LK", "swedish": "SE", "swiss": "CH",
    "syrian": "SY", "taiwanese": "TW", "tajik": "TJ", "thai": "TH", "tunisian": "TN",
    "turkish": "TR", "turkmen": "TM", "ukrainian": "UA", "uzbek": "UZ", "venezuelan": "VE",
    "vietnamese": "VN", "welsh": "GB", "yemeni": "YE", "zimbabwean": "ZW",
    "citizen of the united states": "US", "united states": "US", "united kingdom": "GB",
}  # fmt: skip


def normalise_nationality(value: str | None) -> str | None:
    """`British` -> `GB`, `ru` -> `RU`. None for anything that names no one country."""
    if value is None:
        return None
    word = " ".join(value.strip().casefold().split())
    return NATIONALITY_WORDS.get(word) or normalise_jurisdiction(value)
