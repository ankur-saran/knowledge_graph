"""Sample files in the published formats of GLEIF and OpenSanctions, for offline tests.

The files in `tests/data/real_format/` are written by `build_samples()` and
committed; `test_real_loaders.py` checks they are what it writes. To change
them, change this module and run `uv run python tests/real_format.py`.

The layout is a refresh directory as `ubo download` writes it. GLEIF files have
GLEIF's full headers, quote every field and end lines with CRLF, as GLEIF's
golden copy does; the OpenSanctions file is FtM NDJSON as published. Every
name, LEI and id is invented (LEIs start `FXR`); nothing here is real data.

The world: a group with a branch (Brackwater Velling); a designated US-DE
company that a German subsidiary consolidates into and that OpenSanctions
names by its LEI (Orvasko); a fund family with every fund relation
(Tamsworth Quillon); a sole proprietor; a duplicate LEI and its successor; a
lapsed LEI whose 60 % owner is designated only by the EU (Halvenstrand,
Northgate); a name-only match with a jurisdiction (Sarvanta) and one without
(Mirovane); inactive relationships; an ended designation; a vessel; a target
with no designation record; ownership percentages written every way.
"""

import csv
import hashlib
import io
import json
from pathlib import Path

SAMPLES = Path("tests/data/real_format")

GLEIF_PUBLISHED = "2026-10-01T00:00:00"
OS_PUBLISHED = "2026-10-01T06:00:00"


def lei(tag: str) -> str:
    """A 20-character LEI of the sample world."""
    assert len(tag) <= 15, tag
    return f"FXR{tag}".ljust(18, "0") + "00"


# --- GLEIF headers, in GLEIF's order ----------------------------------------------------

_ADDRESS = [
    "FirstAddressLine",
    "AddressNumber",
    "AddressNumberWithinBuilding",
    "MailRouting",
    "AdditionalAddressLine.1",
    "AdditionalAddressLine.2",
    "AdditionalAddressLine.3",
    "City",
    "Region",
    "Country",
    "PostalCode",
]


def _lei2_header() -> list[str]:
    h = ["LEI", "Entity.LegalName", "Entity.LegalName.xmllang"]
    for group, item in (
        ("OtherEntityNames", "OtherEntityName"),
        ("TransliteratedOtherEntityNames", "TransliteratedOtherEntityName"),
    ):
        for i in range(1, 6):
            name = f"Entity.{group}.{item}.{i}"
            h += [name, f"{name}.xmllang", f"{name}.type"]
    for address in ("LegalAddress", "HeadquartersAddress"):
        h += [f"Entity.{address}.{f}" for f in ["xmllang", *_ADDRESS]]
    for group, item in (
        ("OtherAddresses", "OtherAddress"),
        ("TransliteratedOtherAddresses", "TransliteratedOtherAddress"),
    ):
        for i in range(1, 6):
            h += [f"Entity.{group}.{item}.{i}.{f}" for f in ["xmllang", "type", *_ADDRESS]]
    h += [
        "Entity.RegistrationAuthority.RegistrationAuthorityID",
        "Entity.RegistrationAuthority.OtherRegistrationAuthorityID",
        "Entity.RegistrationAuthority.RegistrationAuthorityEntityID",
        "Entity.LegalJurisdiction",
        "Entity.EntityCategory",
        "Entity.EntitySubCategory",
        "Entity.LegalForm.EntityLegalFormCode",
        "Entity.LegalForm.OtherLegalForm",
        "Entity.AssociatedEntity.type",
        "Entity.AssociatedEntity.AssociatedLEI",
        "Entity.AssociatedEntity.AssociatedEntityName",
        "Entity.AssociatedEntity.AssociatedEntityName.xmllang",
        "Entity.EntityStatus",
        "Entity.EntityCreationDate",
        "Entity.EntityExpirationDate",
        "Entity.EntityExpirationReason",
    ]
    for i in range(1, 6):
        h += [
            f"Entity.SuccessorEntity.{i}.SuccessorLEI",
            f"Entity.SuccessorEntity.{i}.SuccessorEntityName",
            f"Entity.SuccessorEntity.{i}.SuccessorEntity.xmllang",
        ]
    for i in range(1, 6):
        event = f"Entity.LegalEntityEvents.LegalEntityEvent.{i}"
        h += [
            f"{event}.{f}"
            for f in (
                "group_type",
                "event_status",
                "group_id",
                "group_sequence_no",
                "LegalEntityEventType",
                "LegalEntityEventEffectiveDate",
                "LegalEntityEventRecordedDate",
                "ValidationDocuments",
                "ValidationReference",
            )
        ]
        for j in range(1, 6):
            h += [
                f"{event}.AffectedFields.{j}.AffectedField",
                f"{event}.AffectedFields.{j}.field_xpath",
            ]
    h += [
        "Registration.InitialRegistrationDate",
        "Registration.LastUpdateDate",
        "Registration.RegistrationStatus",
        "Registration.NextRenewalDate",
        "Registration.ManagingLOU",
        "Registration.ValidationSources",
        "Registration.ValidationAuthority.ValidationAuthorityID",
        "Registration.ValidationAuthority.OtherValidationAuthorityID",
        "Registration.ValidationAuthority.ValidationAuthorityEntityID",
    ]
    for i in range(1, 6):
        other = f"Registration.OtherValidationAuthorities.OtherValidationAuthority.{i}"
        h += [
            f"{other}.ValidationAuthorityID",
            f"{other}.OtherValidationAuthorityID",
            f"{other}.ValidationAuthorityEntityID",
        ]
    return [*h, "ConformityFlag"]


def _rr_header() -> list[str]:
    r = "Relationship"
    h = [
        f"{r}.StartNode.NodeID",
        f"{r}.StartNode.NodeIDType",
        f"{r}.EndNode.NodeID",
        f"{r}.EndNode.NodeIDType",
        f"{r}.RelationshipType",
        f"{r}.RelationshipStatus",
    ]
    for i in range(1, 6):
        h += [f"{r}.Period.{i}.startDate", f"{r}.Period.{i}.endDate", f"{r}.Period.{i}.periodType"]
    for i in range(1, 6):
        h += [f"{r}.Qualifiers.{i}.QualifierDimension", f"{r}.Qualifiers.{i}.QualifierCategory"]
    for i in range(1, 6):
        h += [
            f"{r}.Quantifiers.{i}.MeasurementMethod",
            f"{r}.Quantifiers.{i}.QuantifierAmount",
            f"{r}.Quantifiers.{i}.QuantifierUnits",
        ]
    return [
        *h,
        "Registration.InitialRegistrationDate",
        "Registration.LastUpdateDate",
        "Registration.RegistrationStatus",
        "Registration.NextRenewalDate",
        "Registration.ManagingLOU",
        "Registration.ValidationSources",
        "Registration.ValidationDocuments",
        "Registration.ValidationReference",
        "DeletedAt",
    ]


def _repex_header() -> list[str]:
    return [
        "LEI",
        "Exception.Category",
        *(f"Exception.Reason.{i}" for i in range(1, 6)),
        *(f"Exception.Reference.{i}" for i in range(1, 6)),
        "DeletedAt",
    ]


LEI2_HEADER = _lei2_header()
RR_HEADER = _rr_header()
REPEX_HEADER = _repex_header()

# --- GLEIF records ----------------------------------------------------------------------

_LOU = "5493001KJTIIGC8Y1R12"
_COUNTRY_RA = {
    "GB": "RA000585",
    "DE": "RA000242",
    "US": "RA000602",
    "ES": "RA000780",
    "SG": "RA000523",
    "LU": "RA000432",
    "KY": "RA000087",
    "FR": "RA000189",
    "NO": "RA000472",
    "GR": "RA000221",
    "AE": "RA000774",
    "CY": "RA000161",
}


def _entity(
    tag: str,
    name: str,
    jurisdiction: str,
    *,
    city: str,
    status: str = "ACTIVE",
    registration: str = "ISSUED",
    category: str = "GENERAL",
    **cells: str,
) -> dict[str, str]:
    country = jurisdiction.split("-")[0]
    row = {
        "LEI": lei(tag),
        "Entity.LegalName": name,
        "Entity.LegalName.xmllang": "en",
        "Entity.LegalAddress.xmllang": "en",
        "Entity.LegalAddress.FirstAddressLine": f"{len(name)} Harbour Street",
        "Entity.LegalAddress.City": city,
        "Entity.LegalAddress.Country": country,
        "Entity.LegalAddress.PostalCode": f"{len(tag)}{len(name):03d}",
        "Entity.HeadquartersAddress.xmllang": "en",
        "Entity.HeadquartersAddress.FirstAddressLine": f"{len(name)} Harbour Street",
        "Entity.HeadquartersAddress.City": city,
        "Entity.HeadquartersAddress.Country": country,
        "Entity.HeadquartersAddress.PostalCode": f"{len(tag)}{len(name):03d}",
        "Entity.RegistrationAuthority.RegistrationAuthorityID": _COUNTRY_RA[country],
        "Entity.RegistrationAuthority.RegistrationAuthorityEntityID": f"FXR-{tag}",
        "Entity.LegalJurisdiction": jurisdiction,
        "Entity.EntityCategory": category,
        "Entity.LegalForm.EntityLegalFormCode": "8888",
        "Entity.EntityStatus": status,
        "Entity.EntityCreationDate": "2011-05-03T00:00:00+00:00",
        "Registration.InitialRegistrationDate": "2014-02-11T09:30:00+00:00",
        "Registration.LastUpdateDate": "2026-09-22T11:04:51+00:00",
        "Registration.RegistrationStatus": registration,
        "Registration.NextRenewalDate": "2027-02-11T09:30:00+00:00",
        "Registration.ManagingLOU": _LOU,
        "Registration.ValidationSources": "FULLY_CORROBORATED",
        "Registration.ValidationAuthority.ValidationAuthorityID": _COUNTRY_RA[country],
        "Registration.ValidationAuthority.ValidationAuthorityEntityID": f"FXR-{tag}",
        "ConformityFlag": "CONFORMING",
    }
    row.update(cells)
    return row


_OTHER = "Entity.OtherEntityNames.OtherEntityName"
_TRANSLIT = "Entity.TransliteratedOtherEntityNames.TransliteratedOtherEntityName"

LEI2_ROWS = [
    _entity("HOLD", "Brackwater Velling Holdings plc", "GB", city="London"),
    _entity(
        "MID",
        "Brackwater Velling Finance Ltd",
        "GB",
        city="London",
        **{
            f"{_OTHER}.1": "Velling Finance",
            f"{_OTHER}.1.xmllang": "en",
            f"{_OTHER}.1.type": "TRADING_OR_OPERATING_NAME",
        },
    ),
    _entity(
        "SUB",
        "Velling Energía S.A.",
        "ES",
        city="Madrid",
        **{
            "Entity.LegalName.xmllang": "es",
            f"{_OTHER}.1": "Velling Energia SA",
            f"{_OTHER}.1.xmllang": "es",
            f"{_OTHER}.1.type": "ALTERNATIVE_LANGUAGE_LEGAL_NAME",
            f"{_OTHER}.2": "Velling Iberia S.A.",
            f"{_OTHER}.2.xmllang": "es",
            f"{_OTHER}.2.type": "PREVIOUS_LEGAL_NAME",
        },
    ),
    _entity(
        "BRANCH",
        "Brackwater Velling Finance Ltd, Singapore Branch",
        "SG",
        city="Singapore",
        **{"Entity.EntityCategory": "BRANCH"},
    ),
    _entity("ORV", "Orvasko Bulk Logistics LLC", "US-DE", city="Wilmington"),
    _entity(
        "ORVDE",
        "Orvasko Bulk Logistics GmbH",
        "DE",
        city="Hamburg",
        **{"Entity.LegalName.xmllang": "de"},
    ),
    _entity(
        "FUND",
        "Tamsworth Quillon Opportunities Fund",
        "LU",
        city="Luxembourg",
        category="FUND",
        **{"Entity.LegalForm.OtherLegalForm": "FUND"},
    ),
    _entity("MGR", "Tamsworth Quillon Asset Management S.A.", "LU", city="Luxembourg"),
    _entity(
        "SUBF",
        "Tamsworth Quillon Opportunities Fund - Sub-Fund A",
        "LU",
        city="Luxembourg",
        category="FUND",
    ),
    _entity("FEED", "Tamsworth Quillon Feeder Fund Ltd", "KY", city="George Town", category="FUND"),
    _entity(
        "SOLE",
        "Marguerite Delacroix-Venn",
        "FR",
        city="Lyon",
        category="SOLE_PROPRIETOR",
        # No registration authority: GLEIF's code for none, and no number.
        **{
            "Entity.LegalName.xmllang": "fr",
            "Entity.RegistrationAuthority.RegistrationAuthorityID": "RA999999",
            "Entity.RegistrationAuthority.RegistrationAuthorityEntityID": "",
        },
    ),
    _entity(
        "DUP",
        "Kolmhaven Maschinenbau GmbH",
        "DE",
        city="Bremen",
        status="INACTIVE",
        registration="DUPLICATE",
        # One company registered twice: the same register entry as its successor.
        **{
            "Entity.SuccessorEntity.1.SuccessorLEI": lei("KOLM"),
            "Entity.SuccessorEntity.1.SuccessorEntityName": "Kolmhaven Maschinenbau GmbH",
            "Entity.RegistrationAuthority.RegistrationAuthorityEntityID": "FXR-KOLM",
        },
    ),
    _entity("KOLM", "Kolmhaven Maschinenbau GmbH", "DE", city="Bremen"),
    _entity("HALV", "Halvenstrand Shipping AS", "NO", city="Bergen", registration="LAPSED"),
    _entity(
        "ELS",
        "Elsworth Pine Timber Ltd",
        "GB",
        city="Leeds",
        status="INACTIVE",
        registration="RETIRED",
    ),
    _entity(
        "ARDV",
        "Ardevique Logistique SARL",
        "FR",
        city="Marseille",
        status="NULL",
        # A register GLEIF does not list, named in free text.
        **{
            "Entity.LegalName.xmllang": "fr",
            "Entity.RegistrationAuthority.RegistrationAuthorityID": "RA888888",
            "Entity.RegistrationAuthority.OtherRegistrationAuthorityID": "Greffe de Marseille",
        },
    ),
    _entity(
        "THAL",
        "ΘΑΛΑΣΣΟΒΙΑ ΝΑΥΤΙΛΙΑΚΗ Α.Ε.",
        "GR",
        city="Πειραιάς",
        **{
            "Entity.LegalName.xmllang": "el",
            f"{_TRANSLIT}.1": "THALASSOVIA NAFTILIAKI A.E.",
            f"{_TRANSLIT}.1.xmllang": "el",
            f"{_TRANSLIT}.1.type": "PREFERRED_ASCII_TRANSLITERATED_LEGAL_NAME",
        },
    ),
    _entity(
        "PEMB",
        "Pemberwick, Hale & Co. LLP",
        "GB",
        city="Bristol",
        **{
            f"{_OTHER}.1": '"Pemberwick" Advisory',
            f"{_OTHER}.1.xmllang": "en",
            f"{_OTHER}.1.type": "TRADING_OR_OPERATING_NAME",
        },
    ),
    _entity("SARV", "Sarvanta Metals Trading LLC", "AE", city="Dubai"),
    _entity("MIRO", "Mirovane Industrial Group Ltd", "CY", city="Limassol"),
]


def _relationship(
    start: str,
    end: str,
    rel_type: str,
    status: str = "ACTIVE",
    percentage: str | None = None,
    **cells: str,
) -> dict[str, str]:
    r = "Relationship"
    row = {
        f"{r}.StartNode.NodeID": lei(start),
        f"{r}.StartNode.NodeIDType": "LEI",
        f"{r}.EndNode.NodeID": lei(end),
        f"{r}.EndNode.NodeIDType": "LEI",
        f"{r}.RelationshipType": rel_type,
        f"{r}.RelationshipStatus": status,
        f"{r}.Period.1.startDate": "2019-01-01T00:00:00Z",
        f"{r}.Period.1.periodType": "RELATIONSHIP_PERIOD",
        "Registration.InitialRegistrationDate": "2019-03-04T00:00:00Z",
        "Registration.LastUpdateDate": "2026-09-22T11:04:51Z",
        "Registration.RegistrationStatus": "PUBLISHED",
        "Registration.NextRenewalDate": "2027-03-04T00:00:00Z",
        "Registration.ManagingLOU": _LOU,
        "Registration.ValidationSources": "FULLY_CORROBORATED",
        "Registration.ValidationDocuments": "ACCOUNTS_FILING",
    }
    if rel_type.endswith("CONSOLIDATED_BY"):
        row |= {
            f"{r}.Period.2.startDate": "2025-01-01T00:00:00Z",
            f"{r}.Period.2.endDate": "2025-12-31T00:00:00Z",
            f"{r}.Period.2.periodType": "ACCOUNTING_PERIOD",
            f"{r}.Qualifiers.1.QualifierDimension": "ACCOUNTING_STANDARD",
            f"{r}.Qualifiers.1.QualifierCategory": "IFRS",
        }
    if percentage is not None:
        row |= {
            f"{r}.Quantifiers.1.MeasurementMethod": "ACCOUNTING_CONSOLIDATION",
            f"{r}.Quantifiers.1.QuantifierAmount": percentage,
            f"{r}.Quantifiers.1.QuantifierUnits": "PERCENTAGE",
        }
    row.update(cells)
    return row


RR_ROWS = [
    _relationship("MID", "HOLD", "IS_DIRECTLY_CONSOLIDATED_BY", percentage="100.00"),
    _relationship("MID", "HOLD", "IS_ULTIMATELY_CONSOLIDATED_BY"),
    _relationship("SUB", "MID", "IS_DIRECTLY_CONSOLIDATED_BY"),
    _relationship("SUB", "HOLD", "IS_ULTIMATELY_CONSOLIDATED_BY"),
    _relationship("BRANCH", "MID", "IS_INTERNATIONAL_BRANCH_OF"),
    _relationship("ORVDE", "ORV", "IS_DIRECTLY_CONSOLIDATED_BY", percentage="100.00"),
    _relationship("ORVDE", "ORV", "IS_ULTIMATELY_CONSOLIDATED_BY"),
    _relationship("FUND", "MGR", "IS_FUND-MANAGED_BY"),
    _relationship("SUBF", "FUND", "IS_SUBFUND_OF"),
    _relationship("FEED", "FUND", "IS_FEEDER_TO"),
    # Ended relationships: GLEIF writes INACTIVE, and sometimes the word NULL.
    _relationship(
        "ELS",
        "HOLD",
        "IS_DIRECTLY_CONSOLIDATED_BY",
        status="INACTIVE",
        **{"Relationship.Period.1.endDate": "2023-06-30T00:00:00Z"},
    ),
    _relationship("ARDV", "HOLD", "IS_DIRECTLY_CONSOLIDATED_BY", status="NULL"),
    # A parent with no LEI record in the file.
    _relationship("PEMB", "GHOST", "IS_DIRECTLY_CONSOLIDATED_BY"),
]


def _exception(tag: str, category: str, *reasons: str, **cells: str) -> dict[str, str]:
    row = {"LEI": lei(tag), "Exception.Category": category}
    row |= {f"Exception.Reason.{i}": reason for i, reason in enumerate(reasons, start=1)}
    row.update(cells)
    return row


_DIRECT = "DIRECT_ACCOUNTING_CONSOLIDATION_PARENT"
_ULTIMATE = "ULTIMATE_ACCOUNTING_CONSOLIDATION_PARENT"

REPEX_ROWS = [
    _exception("HOLD", _DIRECT, "NATURAL_PERSONS"),
    _exception("HOLD", _ULTIMATE, "NATURAL_PERSONS"),
    _exception("SOLE", _DIRECT, "NATURAL_PERSONS"),
    _exception("SOLE", _ULTIMATE, "NATURAL_PERSONS"),
    _exception("MGR", _DIRECT, "NON_CONSOLIDATING", "NO_KNOWN_PERSON"),
    _exception("MGR", _ULTIMATE, "NON_CONSOLIDATING"),
    _exception("HALV", _DIRECT, "NO_LEI"),
    _exception(
        "KOLM",
        _ULTIMATE,
        "NON_PUBLIC",
        **{"Exception.Reference.1": "Confidentiality under a shareholder agreement"},
    ),
    _exception("SARV", _DIRECT, "CONSENT_NOT_OBTAINED"),
    # Withdrawn by the entity: GLEIF keeps the record with the time it was deleted.
    _exception("THAL", _DIRECT, "NO_KNOWN_PERSON", DeletedAt="2026-09-30T02:53:17Z"),
]


def _csv(header: list[str], rows: list[dict[str, str]]) -> bytes:
    for row in rows:
        unknown = set(row) - set(header)
        assert not unknown, unknown
    out = io.StringIO()
    writer = csv.writer(out, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([row.get(column, "") for column in header])
    return out.getvalue().encode("utf-8")


# --- OpenSanctions ----------------------------------------------------------------------


def _ftm(
    entity_id: str,
    schema: str,
    caption: str,
    properties: dict[str, list[str]],
    datasets: list[str],
    target: bool = False,
    referents: list[str] | None = None,
) -> dict:
    return {
        "id": entity_id,
        "caption": caption,
        "schema": schema,
        "referents": referents or [],
        "datasets": datasets,
        "first_seen": "2024-02-14T10:27:20",
        "last_seen": "2026-10-01T05:41:12",
        "last_change": "2026-08-19T16:10:01",
        "properties": properties,
        "target": target,
    }


_SDN, _EU, _UK = "us_ofac_sdn", "eu_fsf", "gb_hmt_sanctions"


def _company(entity_id, name, datasets, schema="Company", target=True, **props):
    properties = {"name": [name], "topics": ["sanction"] if target else []} | props
    properties = {k: v for k, v in properties.items() if v}
    return _ftm(entity_id, schema, name, properties, datasets, target)


def _sanction(entity_id, of, dataset, program_id, **props):
    properties = {
        "entity": [of],
        "authority": [
            {_SDN: "Office of Foreign Assets Control", _EU: "European Union", _UK: "HM Treasury"}[
                dataset
            ]
        ],
        "country": [{_SDN: "us", _EU: "eu", _UK: "gb"}[dataset]],
        "programId": [program_id],
        "sourceUrl": [f"https://sanctions.example.invalid/{dataset}/{entity_id}"],
    } | props
    return _ftm(entity_id, "Sanction", program_id, properties, [dataset])


def _ownership(entity_id, owner, asset, dataset, percentage=None, role="Owned or Controlled By"):
    properties = {"owner": [owner], "asset": [asset], "role": [role]}
    if percentage is not None:
        properties["percentage"] = [percentage]
    return _ftm(entity_id, "Ownership", "Ownership", properties, [dataset])


OS_ENTITIES = [
    # Named by its LEI; designated by OFAC.
    _company(
        "NK-fxOrvasko",
        "Orvasko Bulk Logistics LLC",
        [_SDN],
        leiCode=[lei("ORV")],
        country=["us"],
        alias=["Orvasko Logistics", "Орваско Балк Логистикс"],
        registrationNumber=["FXR-ORV"],
    ),
    _sanction(
        "os-fx-orv-sdn",
        "NK-fxOrvasko",
        _SDN,
        "US-FXR-EO1",
        program=["Executive Order FX-1"],
        startDate=["2024-03-11"],
        listingDate=["2024-03-11"],
    ),
    # No LEI: matched by name and jurisdiction.
    _company(
        "NK-fxSarvanta",
        "Sarvanta Metals Trading L.L.C.",
        [_SDN],
        jurisdiction=["ae"],
        country=["ae"],
        weakAlias=["Sarvanta"],
        # Shared with its refinery below (Step 14's addresses).
        address=["Office 1204, Gold Tower, Jumeirah Lakes Towers, Dubai"],
    ),
    _sanction("os-fx-sarv-sdn", "NK-fxSarvanta", _SDN, "US-FXR-EO2", startDate=["2025-01-15"]),
    # No LEI and no country: a name alone.
    _company("NK-fxMirovane", "Mirovane Industrial Group", [_SDN], schema="Organization"),
    _sanction("os-fx-miro-sdn", "NK-fxMirovane", _SDN, "US-FXR-EO2", listingDate=["2025-05-02"]),
    # Designated only by the EU; owns 60 % of a company with a lapsed LEI.
    _company(
        "NK-fxNorthgate",
        "Northgate Petroleum Trading FZE",
        [_EU],
        country=["ae"],
        alias=['Northgate "NPT" Trading', "Northgate Petroleum Trading\\FZE"],
    ),
    _sanction(
        "os-fx-north-eu",
        "NK-fxNorthgate",
        _EU,
        "EU-FXR-REG1",
        startDate=["2023-07-21"],
        reason=["Supplies the sanctioned sector."],
    ),
    _company(
        "NK-fxHalvenstrand",
        "Halvenstrand Shipping AS",
        [_EU],
        target=False,
        leiCode=[lei("HALV")],
        country=["no"],
    ),
    _ownership("NK-fxOwn1", "NK-fxNorthgate", "NK-fxHalvenstrand", _EU, percentage="60"),
    # Percentages as they are written.
    _company("NK-fxOrvMarine", "Orvasko Marine Services Ltd", [_SDN], target=False, country=["cy"]),
    _ownership("NK-fxOwn2", "NK-fxOrvasko", "NK-fxOrvMarine", _SDN, percentage="50%"),
    _company(
        "NK-fxSarvRefining",
        "Sarvanta Refining FZCO",
        [_SDN],
        target=False,
        country=["ae"],
        address=["Office 1204 Gold Tower, Jumeirah Lakes Towers, Dubai"],
    ),
    _ownership("NK-fxOwn3", "NK-fxSarvanta", "NK-fxSarvRefining", _SDN, percentage="25-50"),
    _company("NK-fxMiroTrading", "Mirovane Trading Kft.", [_SDN], target=False, country=["hu"]),
    _ownership("NK-fxOwn4", "NK-fxMirovane", "NK-fxMiroTrading", _SDN, percentage="majority"),
    _company("NK-fxSarvLogistics", "Sarvanta Logistics LLC", [_SDN], target=False, country=["ae"]),
    _ownership("NK-fxOwn5", "NK-fxSarvanta", "NK-fxSarvLogistics", _SDN),
    _company("NK-fxNorthBunker", "Northgate Bunkering Ltd", [_EU], target=False, country=["mt"]),
    _ownership("NK-fxOwn6", "NK-fxNorthgate", "NK-fxNorthBunker", _EU, percentage="50.0"),
    # A designated person who directs a designated company.
    _ftm(
        "NK-fxLomanov",
        "Person",
        "Viktor Lomanov",
        {
            "name": ["Viktor Lomanov"],
            "alias": ["Viktor Lomanoff"],
            "birthDate": ["1966-04-12"],
            "nationality": ["ae"],
            "topics": ["sanction"],
        },
        [_SDN],
        target=True,
    ),
    _sanction("os-fx-lomanov-sdn", "NK-fxLomanov", _SDN, "US-FXR-EO2", startDate=["2025-01-15"]),
    _ftm(
        "NK-fxDir1",
        "Directorship",
        "Director",
        {"director": ["NK-fxLomanov"], "organization": ["NK-fxSarvanta"], "role": ["Director"]},
        [_SDN],
    ),
    # Delisted: the designation ended before the publish date.
    _company("NK-fxTeverin", "Teverin Grain Export LLC", [_UK], country=["ua"]),
    _sanction(
        "os-fx-teverin-uk",
        "NK-fxTeverin",
        _UK,
        "UK-FXR-REG2",
        startDate=["2022-03-15"],
        endDate=["2026-03-31"],
    ),
    # A designation that ends after the publish date is still in force.
    _company(
        "NK-fxKestovar",
        "Kestovar Rail Components JSC",
        [_UK],
        schema="LegalEntity",
        country=["by"],
        ogrnCode=["1027700000000"],
        innCode=["7700000000"],
    ),
    _sanction(
        "os-fx-kestovar-uk",
        "NK-fxKestovar",
        _UK,
        "UK-FXR-REG2",
        startDate=["2024-02-24"],
        endDate=["2027-12-31"],
    ),
    _company(
        "NK-fxPortAuthority",
        "Port Authority of Velmora",
        [_EU],
        schema="PublicBody",
        country=["vm"],
    ),
    _sanction("os-fx-port-eu", "NK-fxPortAuthority", _EU, "EU-FXR-REG1", startDate=["2023-07-21"]),
    # A target with no designation record.
    _company("NK-fxQuillmarsh", "Quillmarsh Capital Partners", [_SDN], country=["pa"]),
    # Not entities the graph holds: a vessel and its designation, an address,
    # an identification and a link of a schema no mapping reads.
    _ftm(
        "NK-fxVessel",
        "Vessel",
        "NORTHGATE STAR",
        {
            "name": ["NORTHGATE STAR"],
            "imoNumber": ["IMO9000001"],
            "flag": ["pa"],
            "topics": ["sanction"],
        },
        [_SDN],
        target=True,
    ),
    _sanction("os-fx-vessel-sdn", "NK-fxVessel", _SDN, "US-FXR-EO2", startDate=["2025-01-15"]),
    _ftm(
        "addr-fx-1",
        "Address",
        "Harbour Street, Dubai",
        {"full": ["Harbour Street, Dubai"], "country": ["ae"]},
        [_SDN],
    ),
    _ftm(
        "NK-fxIdent1",
        "Identification",
        "FXR-ORV",
        {"holder": ["NK-fxOrvasko"], "number": ["FXR-ORV"], "type": ["Registration"]},
        [_SDN],
    ),
    _ftm(
        "NK-fxLink1",
        "UnknownLink",
        "Associate",
        {"subject": ["NK-fxNorthgate"], "object": ["NK-fxSarvanta"], "role": ["Associate"]},
        [_EU],
    ),
]


def _ndjson(records: list[dict]) -> bytes:
    return b"".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        for record in records
    )


# --- the refresh directory --------------------------------------------------------------

_GLEIF_URL = "https://goldencopy.gleif.org/storage/golden-copy-files/2026/10/01/fx/"
_OS_URL = "https://data.opensanctions.org/artifacts/sanctions/20261001060000-fxr/"

FILES = {
    "gleif_lei2": (
        "gleif",
        "gleif/lei2.csv",
        _GLEIF_URL + "lei2-golden-copy.csv.zip",
        GLEIF_PUBLISHED,
        "CC0-1.0",
    ),
    "gleif_rr": (
        "gleif",
        "gleif/rr.csv",
        _GLEIF_URL + "rr-golden-copy.csv.zip",
        GLEIF_PUBLISHED,
        "CC0-1.0",
    ),
    "gleif_repex": (
        "gleif",
        "gleif/repex.csv",
        _GLEIF_URL + "repex-golden-copy.csv.zip",
        GLEIF_PUBLISHED,
        "CC0-1.0",
    ),
    "opensanctions_entities": (
        "opensanctions",
        "opensanctions/entities.ftm.json",
        _OS_URL + "entities.ftm.json",
        OS_PUBLISHED,
        "CC-BY-NC-4.0",
    ),
}


def build_samples() -> dict[str, bytes]:
    """Relative path -> bytes of every file of the sample refresh directory."""
    content = {
        "gleif/lei2.csv": _csv(LEI2_HEADER, LEI2_ROWS),
        "gleif/rr.csv": _csv(RR_HEADER, RR_ROWS),
        "gleif/repex.csv": _csv(REPEX_HEADER, REPEX_ROWS),
        "opensanctions/entities.ftm.json": _ndjson(OS_ENTITIES),
    }
    files = {}
    for key, (source, path, url, published_at, licence) in FILES.items():
        data = content[path]
        files[key] = {
            "path": path,
            "source": source,
            "url": url,
            "published_at": published_at,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "publisher_checksum": (
                "sha1:" + hashlib.sha1(data).hexdigest() if source == "opensanctions" else None
            ),
            "archive_sha256": None,
            "licence": licence,
        }
    manifest = {"manifest_version": 1, "files": files}
    content["manifest.json"] = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode()
    return content


def write_samples(directory: Path = SAMPLES) -> None:
    for path, data in build_samples().items():
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def restamp(directory):
    """Record the files' new bytes in the manifest after a test changed them."""
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["files"].values():
        data = (directory / entry["path"]).read_bytes()
        entry["size"] = len(data)
        entry["sha256"] = hashlib.sha256(data).hexdigest()
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


if __name__ == "__main__":
    write_samples()
