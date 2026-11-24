"""`ubo sample`: a smaller refresh directory, cut from a full one, closed under references.

A sample is chosen by a seed, so the same refresh, size and seed give the same
files and the same refresh id. It starts from `n` registry records and some
list targets, then takes, until nothing more is added:

- the parents its relationship records name, and the successor of a replaced LEI;
- the list records that state a chosen LEI, and the LEI a chosen list record states;
- the owners and directors of chosen list records;
- the Companies House companies of chosen LEIs (by registration authority and
  number), some companies with PSCs chosen by the seed, and the UK companies
  their PSCs name (Step 17).

A record is written as published, in file order: GLEIF rows quoted and ended
with CRLF, list lines as they were. Relationships whose two ends are chosen,
the reporting exceptions of chosen LEIs and the designations of chosen list
records come with them, and so do the PSC lines of chosen companies. The
manifest says it is a sample and of what, so its
sets are of the `sample` family and never become `real`.

The sample is its own bytes, so its snapshots have their own ids; a full
ingest of the source files is never mistaken for it.
"""

import csv
import io
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import duckdb

from ubo_sentinel.models.manifest import Manifest, ManifestFile, SampleOf, load_manifest
from ubo_sentinel.pipeline.bronze import file_sha256
from ubo_sentinel.pipeline.loaders.companies_house_loader import COMPANIES_FILE, PSC_FILE
from ubo_sentinel.pipeline.loaders.gleif_loader import GLEIF_L1, GLEIF_REPEX, GLEIF_RR
from ubo_sentinel.pipeline.loaders.opensanctions_loader import FILE as OS_FILE
from ubo_sentinel.pipeline.mappers import (
    CH_AUTHORITY,
    CH_AUTHORITY_NI,
    CH_CORPORATE,
    OS_ENTITY_SCHEMAS,
    OS_LINK_SCHEMAS,
)

_LEI = "LEI"
_SUCCESSOR = "Entity.SuccessorEntity.1.SuccessorLEI"
_START = "Relationship.StartNode.NodeID"
_END = "Relationship.EndNode.NodeID"
_AUTHORITY = "Entity.RegistrationAuthority.RegistrationAuthorityID"
_AUTHORITY_NUMBER = "Entity.RegistrationAuthority.RegistrationAuthorityEntityID"
_CH_NUMBER = " CompanyNumber"
_CH_FILES = (COMPANIES_FILE, PSC_FILE)
_MAX_LINE = 32 * 1024 * 1024


class SampleError(ValueError):
    """The refresh directory cannot be sampled."""


@dataclass(frozen=True)
class SampleResult:
    refresh_dir: Path
    manifest: Manifest
    # "new" or "already present".
    status: str
    # File key -> records written.
    records: dict[str, int]


def _q(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _header(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        header = next(csv.reader(f), None)
    if not header:
        raise SampleError(f"{path}: has no header row.")
    return header


def _csv_source(path: Path, header: list[str]) -> str:
    types = ", ".join(f"{_q(name)}: 'VARCHAR'" for name in header)
    return (
        f"read_csv({_q(str(path))}, header=true, columns={{{types}}}, auto_detect=false,"
        f" ignore_errors=true, max_line_size={_MAX_LINE}, buffer_size={2 * _MAX_LINE})"
    )


def _lines_source(path: Path) -> str:
    return (
        f"read_csv({_q(str(path))}, header=false, columns={{'line': 'VARCHAR'}},"
        f" delim=E'\\x01', quote='', escape='', auto_detect=false, ignore_errors=true,"
        f" max_line_size={_MAX_LINE}, buffer_size={2 * _MAX_LINE})"
    )


def _first(prop: str) -> str:
    return f"json_extract_string(line, '$.properties.{prop}[0]')"


def _files(refresh_dir: Path, manifest: Manifest) -> dict[str, Path]:
    keys = (GLEIF_L1.file, GLEIF_RR.file, GLEIF_REPEX.file, OS_FILE)
    paths = {}
    # Companies House is optional: sampled when the refresh has both its files.
    if all(key in manifest.files for key in _CH_FILES):
        keys = (*keys, *_CH_FILES)
    for key in keys:
        entry = manifest.files.get(key)
        if entry is None:
            raise SampleError(f"{refresh_dir}: the manifest has no '{key}' file.")
        path = refresh_dir / entry.path
        if not path.is_file() or file_sha256(path) != entry.sha256:
            raise SampleError(f"{path}: missing, or not the bytes the manifest records.")
        paths[key] = path
    return paths


def _choose(
    con: duckdb.DuckDBPyConnection, paths: dict[str, Path], n: int, list_n: int, seed: int
) -> None:
    """Fill `chosen_lei` and `chosen_os`."""
    l1, rr = paths[GLEIF_L1.file], paths[GLEIF_RR.file]
    con.execute(
        f"CREATE TABLE lei AS SELECT {_ident(_LEI)} AS lei, {_ident(_SUCCESSOR)} AS successor,"
        f" {_ident(_AUTHORITY)} AS authority, {_ident(_AUTHORITY_NUMBER)} AS number"
        f" FROM {_csv_source(l1, _header(l1))} WHERE {_ident(_LEI)} IS NOT NULL"
    )
    con.execute(
        f"CREATE TABLE rr AS SELECT {_ident(_START)} AS child, {_ident(_END)} AS parent"
        f" FROM {_csv_source(rr, _header(rr))}"
    )
    # `rowid` of a table is the order its rows were read in: file order.
    con.execute(f"CREATE TABLE os_lines AS SELECT line FROM {_lines_source(paths[OS_FILE])}")
    con.execute(
        "CREATE TABLE os AS SELECT rowid AS rn, line,"
        " json_extract_string(line, '$.id') AS id, json_extract_string(line, '$.schema') AS schema,"
        " coalesce(json_extract_string(line, '$.target') = 'true', false) AS target,"
        f" upper({_first('leiCode')}) AS lei"
        " FROM os_lines WHERE json_valid(line)"
    )
    # Ownership and directorship links: the subject (owner, director) and the object.
    links = " UNION ALL ".join(
        f"SELECT {_first(subject)} AS subject, {_first(obj)} AS object, rn FROM os"
        f" WHERE schema = {_q(schema)}"
        for schema, (_, _, subject, obj) in sorted(OS_LINK_SCHEMAS.items())
    )
    con.execute(f"CREATE TABLE os_links AS {links}")

    entity_schemas = ", ".join(_q(s) for s in sorted(OS_ENTITY_SCHEMAS))
    con.execute(
        "CREATE TABLE chosen_lei AS SELECT lei FROM lei"
        " ORDER BY sha256(CAST(? AS VARCHAR) || ':' || lei) LIMIT ?",
        [seed, n],
    )
    con.execute(
        f"CREATE TABLE chosen_os AS SELECT id FROM os WHERE target AND schema IN ({entity_schemas})"
        " ORDER BY sha256(CAST(? AS VARCHAR) || ':' || id) LIMIT ?",
        [seed, list_n],
    )
    steps = (
        # A list record that states a chosen LEI.
        f"INSERT INTO chosen_os SELECT DISTINCT o.id FROM os o JOIN chosen_lei c ON c.lei = o.lei"
        f" WHERE o.schema IN ({entity_schemas})",
        # The LEI a chosen list record states, when the registry has it.
        "INSERT INTO chosen_lei SELECT DISTINCT o.lei FROM os o JOIN chosen_os c ON c.id = o.id"
        " JOIN lei l ON l.lei = o.lei",
        # The owners and directors of a chosen list record.
        "INSERT INTO chosen_os SELECT DISTINCT k.subject FROM os_links k"
        " JOIN chosen_os c ON c.id = k.object WHERE k.subject IS NOT NULL",
        # The parents a chosen LEI's relationship records name.
        "INSERT INTO chosen_lei SELECT DISTINCT r.parent FROM rr r"
        " JOIN chosen_lei c ON c.lei = r.child JOIN lei l ON l.lei = r.parent",
        # The successor of a replaced LEI.
        "INSERT INTO chosen_lei SELECT DISTINCT l.successor FROM lei l"
        " JOIN chosen_lei c ON c.lei = l.lei JOIN lei s ON s.lei = l.successor",
    )
    size = None
    while True:
        for sql in steps:
            con.execute(sql)
        for table, column in (("chosen_lei", "lei"), ("chosen_os", "id")):
            con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT DISTINCT {column} FROM {table}")
        now = con.execute(
            "SELECT (SELECT count(*) FROM chosen_lei), (SELECT count(*) FROM chosen_os)"
        ).fetchone()
        if now == size:
            break
        size = now
    if COMPANIES_FILE in paths:
        _choose_companies(con, paths, list_n, seed)


def _choose_companies(
    con: duckdb.DuckDBPyConnection, paths: dict[str, Path], list_n: int, seed: int
) -> None:
    """Fill `chosen_ch`: the chosen LEIs' companies, `list_n` companies with PSCs, and the
    UK companies their PSCs name, until nothing more is added."""
    companies = paths[COMPANIES_FILE]
    con.execute(
        f"CREATE TABLE ch AS SELECT {_ident(_CH_NUMBER)} AS number"
        f" FROM {_csv_source(companies, _header(companies))} WHERE {_ident(_CH_NUMBER)} IS NOT NULL"
    )
    con.execute(f"CREATE TABLE psc_lines AS SELECT line FROM {_lines_source(paths[PSC_FILE])}")
    corporate = ", ".join(_q(kind) for kind in CH_CORPORATE)
    con.execute(
        "CREATE TABLE psc AS SELECT rowid AS rn, line,"
        " json_extract_string(line, '$.company_number') AS company,"
        f" CASE WHEN json_extract_string(line, '$.data.kind') IN ({corporate})"
        " THEN upper(replace(trim(json_extract_string(line,"
        " '$.data.identification.registration_number')), ' ', '')) END AS holder"
        " FROM psc_lines WHERE json_valid(line)"
    )
    con.execute(
        "CREATE TABLE chosen_ch AS SELECT DISTINCT c.number FROM ch c"
        " JOIN lei l ON l.number = c.number AND l.authority IN (?, ?)"
        " JOIN chosen_lei k ON k.lei = l.lei",
        [CH_AUTHORITY, CH_AUTHORITY_NI],
    )
    con.execute(
        "INSERT INTO chosen_ch SELECT number FROM ("
        " SELECT DISTINCT company AS number FROM psc WHERE company IN (SELECT number FROM ch))"
        " ORDER BY sha256(CAST(? AS VARCHAR) || ':' || number) LIMIT ?",
        [seed, list_n],
    )
    size = None
    while True:
        # A UK company a chosen company's PSC names, its number as the register writes it.
        con.execute(
            "INSERT INTO chosen_ch SELECT DISTINCT c.number FROM psc p"
            " JOIN chosen_ch k ON k.number = p.company"
            " JOIN ch c ON c.number = CASE WHEN regexp_full_match(p.holder, '[0-9]+')"
            " THEN lpad(p.holder, 8, '0') ELSE p.holder END"
        )
        con.execute("CREATE OR REPLACE TABLE chosen_ch AS SELECT DISTINCT number FROM chosen_ch")
        now = con.execute("SELECT count(*) FROM chosen_ch").fetchone()[0]
        if now == size:
            return
        size = now


def _write_csv(con: duckdb.DuckDBPyConnection, path: Path, key_column: str, into: Path) -> int:
    """The rows of a GLEIF file whose `key_column` (and, for RR, both ends) is chosen."""
    header = _header(path)
    where = f"{_ident(key_column)} IN (SELECT lei FROM chosen_lei)"
    if key_column == _START:
        where += f" AND {_ident(_END)} IN (SELECT lei FROM chosen_lei)"
    rows = con.execute(f"SELECT * FROM {_csv_source(path, header)} WHERE {where}").fetchall()
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(["" if v is None else v for v in row] for row in rows)
    into.parent.mkdir(parents=True, exist_ok=True)
    into.write_bytes(buffer.getvalue().encode("utf-8"))
    return len(rows)


def _write_companies(con: duckdb.DuckDBPyConnection, path: Path, into: Path) -> int:
    """The chosen companies' rows, in file order, under the published header line."""
    header = _header(path)
    rows = con.execute(
        f"SELECT * FROM {_csv_source(path, header)}"
        f" WHERE {_ident(_CH_NUMBER)} IN (SELECT number FROM chosen_ch)"
    ).fetchall()
    with path.open(encoding="utf-8-sig", newline="") as f:
        first = f.readline().rstrip("\r\n")
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerows(["" if v is None else v for v in row] for row in rows)
    into.parent.mkdir(parents=True, exist_ok=True)
    into.write_bytes((first + "\n" + buffer.getvalue()).encode("utf-8"))
    return len(rows)


def _write_psc(con: duckdb.DuckDBPyConnection, into: Path) -> int:
    lines = con.execute(
        "SELECT line FROM psc WHERE company IN (SELECT number FROM chosen_ch) ORDER BY rn"
    ).fetchall()
    into.parent.mkdir(parents=True, exist_ok=True)
    into.write_bytes("".join(line + "\n" for (line,) in lines).encode("utf-8"))
    return len(lines)


def _write_lines(con: duckdb.DuckDBPyConnection, into: Path) -> int:
    links = ", ".join(_q(s) for s in sorted(OS_LINK_SCHEMAS))
    lines = con.execute(
        f"""
        SELECT line FROM os o WHERE o.id IN (SELECT id FROM chosen_os)
           OR (o.schema = 'Sanction' AND EXISTS (
                   SELECT 1 FROM chosen_os c WHERE list_contains(
                       CAST(json_extract(o.line, '$.properties.entity') AS VARCHAR[]), c.id)))
           OR (o.schema IN ({links}) AND o.rn IN (
                   SELECT k.rn FROM os_links k
                   WHERE k.subject IN (SELECT id FROM chosen_os)
                     AND k.object IN (SELECT id FROM chosen_os)))
        ORDER BY o.rn
        """
    ).fetchall()
    into.parent.mkdir(parents=True, exist_ok=True)
    into.write_bytes("".join(line + "\n" for (line,) in lines).encode("utf-8"))
    return len(lines)


def sample(
    refresh_dir: Path, n: int, seed: int, out_root: Path, list_n: int | None = None
) -> SampleResult:
    """Cut a sample of `n` registry records (and `list_n` list targets) into `out_root`."""
    if n < 1:
        raise SampleError("A sample needs at least one record (--n).")
    try:
        source = load_manifest(refresh_dir)
    except (OSError, ValueError) as exc:
        raise SampleError(f"{refresh_dir}: no readable manifest ({exc}).") from exc
    paths = _files(refresh_dir, source)
    list_n = max(1, n // 10) if list_n is None else list_n

    out_root.mkdir(parents=True, exist_ok=True)
    building = out_root / f".sample-{os.getpid()}"
    shutil.rmtree(building, ignore_errors=True)
    con = duckdb.connect()
    try:
        _choose(con, paths, n, list_n, seed)
        written = {}
        for key, column in (
            (GLEIF_L1.file, _LEI),
            (GLEIF_RR.file, _START),
            (GLEIF_REPEX.file, _LEI),
        ):
            written[key] = _write_csv(con, paths[key], column, building / source.files[key].path)
        written[OS_FILE] = _write_lines(con, building / source.files[OS_FILE].path)
        if COMPANIES_FILE in paths:
            written[COMPANIES_FILE] = _write_companies(
                con, paths[COMPANIES_FILE], building / source.files[COMPANIES_FILE].path
            )
            written[PSC_FILE] = _write_psc(con, building / source.files[PSC_FILE].path)
    finally:
        con.close()

    files = {}
    for key in sorted(written):
        original = source.files[key]
        path = building / original.path
        files[key] = ManifestFile(
            path=original.path,
            source=original.source,
            url=original.url,
            published_at=original.published_at,
            size=path.stat().st_size,
            sha256=file_sha256(path),
            licence=original.licence,
        )
    manifest = Manifest(
        files=files,
        sample=SampleOf(refresh_id=source.refresh_id, n=n, list_n=list_n, seed=seed),
    )
    target = out_root / manifest.refresh_id
    if target.exists():
        shutil.rmtree(building)
        return SampleResult(target, manifest, "already present", written)
    manifest.write(building)
    os.replace(building, target)
    return SampleResult(target, manifest, "new", written)
