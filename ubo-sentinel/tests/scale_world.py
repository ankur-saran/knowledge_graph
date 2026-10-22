"""A generated world of registry size, in the published formats, for `test_scale.py`.

A refresh directory as `ubo download` writes it: GLEIF's three files with
GLEIF's headers and quoting, and an OpenSanctions FtM file. Everything is
invented (LEIs start `FXZ`) and follows from the record count alone, so the
same count gives the same bytes.

The registry is `groups` corporate groups of 1 000 companies. In each, company
k > 0 is consolidated by company (k - 1) // 4 of the group, so the leaf k = 999
is five consolidations below the root. Names come from a small vocabulary, as
real names share "holdings" and "trading": blocks of tens of thousands of
names, which is where linking would turn quadratic.

The list holds `list_names` companies that name no registry company (half with
no country), every tenth of them designated by OFAC, and, by group number:

- g % 10 == 0: the root is designated by OFAC, and the list record states its LEI;
- g % 10 == 3: the root is designated by the EU alone, by its LEI;
- g % 10 == 5: an OFAC-designated company owns 60 % of the root;
- g % 10 == 7: company 500 of the group has a namesake on the list, designated
  by OFAC, with no country: a name link, never a merge, and never a block.

The other groups touch no list.
"""

import hashlib
import json
from pathlib import Path

import duckdb
from real_format import LEI2_HEADER, REPEX_HEADER, RR_HEADER

GROUP_SIZE = 1_000
# The leaf of every group: five consolidations below its root.
LEAF = 999

_WORDS = ["Northern", "Atlas", "Velling", "Orchid", "Granite", "Harbour", "Summit", "Crescent"]
_TAILS = ["Holdings", "Trading", "Industries", "Partners", "Logistics", "Capital"]
_LIST_WORDS = ["Zarovsk", "Kemtrel", "Ublinar", "Ostravo", "Pyrenko", "Quellan", "Drovask"]
_PUBLISHED = "2026-10-01T00:00:00"


def lei(i: int) -> str:
    return f"FXZ{i:015d}00"


def name(i: int) -> str:
    return f"{_WORDS[i % 8]} {_WORDS[(i // 8) % 8]} {_TAILS[(i // 64) % 6]} {i} Ltd"


def company(group: int, k: int) -> int:
    return group * GROUP_SIZE + k


def _sql_text(values: list[str]) -> str:
    return "[" + ", ".join("'" + v + "'" for v in values) + "]"


def _copy_csv(path: Path, header: list[str], values: dict[str, str], rows: int) -> None:
    columns = ", ".join(f'{values.get(column, "''")} AS "{column}"' for column in header)
    path.parent.mkdir(parents=True, exist_ok=True)
    duckdb.connect().execute(
        f"COPY (SELECT {columns} FROM range({rows}) t(i) ORDER BY i) TO '{path.as_posix()}'"
        " (HEADER, FORCE_QUOTE *, NEW_LINE '\\r\\n')"
    )


def _gleif(directory: Path, groups: int) -> None:
    n = groups * GROUP_SIZE
    lei_sql = "'FXZ' || lpad(CAST({i} AS VARCHAR), 15, '0') || '00'"
    words, tails = _sql_text(_WORDS), _sql_text(_TAILS)
    _copy_csv(
        directory / "gleif/lei2.csv",
        LEI2_HEADER,
        {
            "LEI": lei_sql.format(i="i"),
            "Entity.LegalName": f"{words}[1 + i % 8] || ' ' || {words}[1 + (i // 8) % 8] || ' '"
            f" || {tails}[1 + (i // 64) % 6] || ' ' || CAST(i AS VARCHAR) || ' Ltd'",
            "Entity.LegalJurisdiction": "CASE WHEN i % 7 = 0 THEN 'US-DE' ELSE 'GB' END",
            "Entity.EntityStatus": "'ACTIVE'",
            "Entity.EntityCategory": "'GENERAL'",
            "Entity.RegistrationAuthority.RegistrationAuthorityID": "'RA000585'",
            "Entity.RegistrationAuthority.RegistrationAuthorityEntityID": (
                "'Z' || CAST(i AS VARCHAR)"
            ),
            "Registration.RegistrationStatus": (
                "CASE WHEN i % 5 = 0 THEN 'LAPSED' ELSE 'ISSUED' END"
            ),
        },
        n,
    )
    # Company k > 0 of a group, consolidated by company (k - 1) // 4 of the same group.
    child = f"(i // {GROUP_SIZE - 1}) * {GROUP_SIZE} + i % {GROUP_SIZE - 1} + 1"
    parent = f"(i // {GROUP_SIZE - 1}) * {GROUP_SIZE} + (i % {GROUP_SIZE - 1}) // 4"
    _copy_csv(
        directory / "gleif/rr.csv",
        RR_HEADER,
        {
            "Relationship.StartNode.NodeID": lei_sql.format(i=child),
            "Relationship.StartNode.NodeIDType": "'LEI'",
            "Relationship.EndNode.NodeID": lei_sql.format(i=parent),
            "Relationship.EndNode.NodeIDType": "'LEI'",
            "Relationship.RelationshipType": "'IS_DIRECTLY_CONSOLIDATED_BY'",
            "Relationship.RelationshipStatus": "'ACTIVE'",
        },
        groups * (GROUP_SIZE - 1),
    )
    # Every root reports no ultimate parent.
    _copy_csv(
        directory / "gleif/repex.csv",
        REPEX_HEADER,
        {
            "LEI": lei_sql.format(i=f"i * {GROUP_SIZE}"),
            "Exception.Category": "'ULTIMATE_ACCOUNTING_CONSOLIDATION_PARENT'",
            "Exception.Reason.1": "'NO_KNOWN_PERSON'",
        },
        groups,
    )


def _ftm(entity_id: str, schema: str, caption: str, properties: dict, datasets: list[str]):
    return {
        "id": entity_id,
        "caption": caption,
        "schema": schema,
        "referents": [],
        "datasets": datasets,
        "properties": properties,
        "target": schema != "Sanction" and "topics" in properties,
    }


def _sanction(of: str, dataset: str) -> dict:
    return _ftm(
        f"{of}-{dataset}",
        "Sanction",
        "Sanction",
        {"entity": [of], "programId": ["FXZ-PROGRAM"], "listingDate": ["2025-01-15"]},
        [dataset],
    )


def _list_records(groups: int, list_names: int):
    for j in range(list_names):
        listed = f"{_LIST_WORDS[j % 7]} {_LIST_WORDS[(j // 7) % 7]} Trading {j}"
        props = {"name": [listed]}
        if j % 2:
            props["country"] = ["ru"]
        if j % 10 == 0:
            props["topics"] = ["sanction"]
        yield _ftm(f"NK-zl{j}", "Company", listed, props, ["us_ofac_sdn"])
        if j % 10 == 0:
            yield _sanction(f"NK-zl{j}", "us_ofac_sdn")
    for g in range(groups):
        root = company(g, 0)
        if g % 10 in (0, 3):
            dataset = "us_ofac_sdn" if g % 10 == 0 else "eu_fsf"
            props = {"name": [name(root)], "leiCode": [lei(root)], "topics": ["sanction"]}
            yield _ftm(f"NK-zr{g}", "Company", name(root), props, [dataset])
            yield _sanction(f"NK-zr{g}", dataset)
        elif g % 10 == 5:
            owner = f"NK-zo{g}"
            yield _ftm(
                owner, "Company", f"Ostravo Kemtrel Owner {g}",
                {"name": [f"Ostravo Kemtrel Owner {g}"], "topics": ["sanction"]}, ["us_ofac_sdn"],
            )  # fmt: skip
            yield _sanction(owner, "us_ofac_sdn")
            asset = f"NK-za{g}"
            yield _ftm(
                asset, "Company", name(root), {"name": [name(root)], "leiCode": [lei(root)]},
                ["us_ofac_sdn"],
            )  # fmt: skip
            yield _ftm(
                f"NK-zw{g}", "Ownership", "Ownership",
                {"owner": [owner], "asset": [asset], "percentage": ["60"]}, ["us_ofac_sdn"],
            )  # fmt: skip
        elif g % 10 == 7:
            twin = company(g, 500)
            props = {"name": [name(twin)], "topics": ["sanction"]}
            yield _ftm(f"NK-zt{g}", "Company", name(twin), props, ["us_ofac_sdn"])
            yield _sanction(f"NK-zt{g}", "us_ofac_sdn")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 22):
            digest.update(chunk)
    return digest.hexdigest()


def write_world(directory: Path, groups: int = 200, list_names: int = 50_000) -> Path:
    """Write the refresh directory; returns it."""
    _gleif(directory, groups)
    ftm = directory / "opensanctions/entities.ftm.json"
    ftm.parent.mkdir(parents=True, exist_ok=True)
    with ftm.open("w", encoding="utf-8", newline="\n") as f:
        for record in _list_records(groups, list_names):
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    files = {}
    for key, path, source, licence in (
        ("gleif_lei2", "gleif/lei2.csv", "gleif", "CC0-1.0"),
        ("gleif_rr", "gleif/rr.csv", "gleif", "CC0-1.0"),
        ("gleif_repex", "gleif/repex.csv", "gleif", "CC0-1.0"),
        (
            "opensanctions_entities",
            "opensanctions/entities.ftm.json",
            "opensanctions",
            "CC-BY-NC-4.0",
        ),
    ):
        file = directory / path
        files[key] = {
            "path": path,
            "source": source,
            "url": f"https://scale.example.invalid/{path}",
            "published_at": _PUBLISHED,
            "size": file.stat().st_size,
            "sha256": _sha256(file),
            "licence": licence,
        }
    manifest = {"manifest_version": 1, "files": files}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return directory
