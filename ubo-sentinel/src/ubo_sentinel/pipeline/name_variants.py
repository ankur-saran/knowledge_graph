"""Spellings of one given name, mapped to one form (Step 19).

Read only for a `Person`, by `name_similarity()`. Each group is one name as it
is commonly romanised; the first spelling is the form the others map to. Every
spelling is written as `normalise_name()` writes it (transliterated, folded, no
punctuation), so a group serves a name in any script.

Kept short on purpose: a variant joins two names that a reader would call one,
and a group that is too wide joins two people. Sources: BGN/PCGN romanisation of
Russian and Ukrainian (1947, 2019) beside the ICAO Doc 9303 passport
transliteration, for the Slavic groups; the common English renderings of Arabic
given names that list records state as aliases of one another, for the rest.
"""

VARIANT_GROUPS: tuple[tuple[str, ...], ...] = (
    # Arabic.
    ("muhammad", "mohammed", "mohamed", "mohammad", "muhammed", "mohamad", "mohamud", "mehmet"),
    ("ahmad", "ahmed", "ahmet"),
    ("husayn", "hussein", "hussain", "husain", "hossein"),
    ("hasan", "hassan"),
    ("abdullah", "abdallah", "abdulla"),
    ("khalid", "khaled"),
    ("mustafa", "mostafa", "moustafa", "mustapha"),
    ("mahmud", "mahmoud"),
    ("umar", "omar"),
    ("usama", "osama"),
    ("yusuf", "youssef", "yousef", "yusef", "yousif"),
    ("ibrahim", "ebrahim"),
    ("abd", "abdul", "abdel"),
    # Russian and Ukrainian.
    ("aleksandr", "alexander", "aleksander", "alexandr", "oleksandr"),
    ("aleksey", "alexey", "aleksei", "alexei", "oleksiy", "oleksii"),
    ("andrey", "andrei", "andriy", "andrii"),
    ("dmitriy", "dmitry", "dmitri", "dmitrii", "dmytro"),
    ("sergey", "sergei", "serguei", "serhiy", "serhii"),
    ("yuriy", "yuri", "yury", "iurii", "yurii"),
    ("yevgeniy", "evgeny", "evgeniy", "yevgeny", "evgenii", "yevhen"),
    ("nikolay", "nikolai", "mykola"),
    ("mikhail", "mikhael", "mykhailo"),
    ("konstantin", "constantin", "kostiantyn"),
    ("viktor", "victor"),
    ("igor", "ihor"),
    ("oleg", "oleh"),
    ("vasiliy", "vasily", "vasili", "vasyl"),
    ("anatoliy", "anatoly", "anatoli", "anatolii"),
    ("arkadiy", "arkady", "arkadii"),
    ("grigoriy", "grigory", "grigori", "hryhoriy"),
    ("valeriy", "valery", "valeri", "valerii"),
    ("gennadiy", "gennady", "gennadi", "hennadiy"),
    ("yekaterina", "ekaterina", "kateryna"),
    ("natalya", "natalia", "nataliya"),
    ("tatyana", "tatiana", "tetiana"),
    ("yuliya", "yulia", "iuliia"),
    ("olga", "olha"),
)

# Spelling -> the form of its group.
GIVEN_NAME_FORMS: dict[str, str] = {
    spelling: group[0] for group in VARIANT_GROUPS for spelling in group
}
