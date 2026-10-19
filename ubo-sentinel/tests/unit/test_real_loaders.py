"""The real-format loader base: published files into Bronze, in SQL."""

import csv
import hashlib
import json
import shutil
from datetime import date

import pytest
from real_format import SAMPLES, build_samples, lei, restamp

from ubo_sentinel.models.canonical import sha256_hex
from ubo_sentinel.models.manifest import Manifest, load_manifest
from ubo_sentinel.pipeline.bronze import BRONZE_TABLES, IngestError, rows_digest
from ubo_sentinel.pipeline.loaders.fixture_loader import ingest_fixtures
from ubo_sentinel.pipeline.loaders.real_base import (
    BAD_VALUE,
    DUPLICATE_KEY,
    MISSING_KEY,
    REJECT_LIMIT_ENV_VAR,
    UNREADABLE,
    Column,
    Format,
    Kind,
    RealDataset,
    ingest_real,
)

TEXT_LIST = Kind.TEXT_LIST

# Declared here for the tests; the GLEIF and OpenSanctions loaders declare their own.
L1 = RealDataset(
    name="gleif_l1",
    table="bronze_entities",
    source="gleif",
    file="gleif_lei2",
    format=Format.CSV,
    columns=tuple(
        Column(name)
        for name in (
            "LEI",
            "Entity.LegalName",
            "Entity.OtherEntityNames.OtherEntityName.1",
            "Entity.OtherEntityNames.OtherEntityName.2",
            "Entity.TransliteratedOtherEntityNames.TransliteratedOtherEntityName.1",
            "Entity.LegalJurisdiction",
            "Entity.EntityCategory",
            "Entity.EntityStatus",
            "Entity.SuccessorEntity.1.SuccessorLEI",
            "Registration.RegistrationStatus",
        )
    ),
    key=("LEI",),
)
RR = RealDataset(
    name="gleif_rr",
    table="bronze_relationships",
    source="gleif",
    file="gleif_rr",
    format=Format.CSV,
    columns=tuple(
        Column(f"Relationship.{name}")
        for name in ("StartNode.NodeID", "EndNode.NodeID", "RelationshipType", "RelationshipStatus")
    ),
    key=(
        "Relationship.StartNode.NodeID",
        "Relationship.EndNode.NodeID",
        "Relationship.RelationshipType",
    ),
)
OS_ENTITIES = RealDataset(
    name="os_entities",
    table="bronze_entities",
    source="opensanctions",
    file="opensanctions_entities",
    format=Format.NDJSON,
    columns=(
        Column("id"),
        Column("schema"),
        Column("target", kind=Kind.BOOL),
        Column("datasets", kind=TEXT_LIST),
        Column("referents", kind=TEXT_LIST),
        *(
            Column(name, within="properties", kind=TEXT_LIST)
            for name in ("name", "alias", "weakAlias", "leiCode", "country", "jurisdiction")
        ),
    ),
    key=("id",),
    select=(
        "schema",
        frozenset({"Company", "Organization", "LegalEntity", "PublicBody", "Person"}),
    ),
)
OS_SANCTIONS = RealDataset(
    name="os_sanctions",
    table="bronze_sanctions",
    source="opensanctions",
    file="opensanctions_entities",
    format=Format.NDJSON,
    columns=(
        Column("id"),
        Column("schema"),
        Column("datasets", kind=TEXT_LIST),
        *(
            Column(name, within="properties", kind=TEXT_LIST)
            for name in ("entity", "programId", "startDate", "endDate", "listingDate")
        ),
    ),
    key=("id",),
    select=("schema", frozenset({"Sanction"})),
)
ALL = (L1, RR, OS_ENTITIES, OS_SANCTIONS)
REQUIRED = [d.name for d in ALL]


def ingest(con, path=SAMPLES, datasets=ALL, required=REQUIRED, base=None):
    return ingest_real(con, path, datasets, required, base=base)


def bronze_rows(con, table, snapshot_id):
    return con.execute(
        f"SELECT _source_record_id, _row_num, _row_hash, CAST(_raw AS VARCHAR), _as_of,"
        f" _confidence, _source FROM {table} WHERE _snapshot_id = ? ORDER BY _row_num",
        [snapshot_id],
    ).fetchall()


def csv_projection(path, dataset):
    """The records of a CSV as the loader should write `_raw`: declared, non-empty cells."""
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return [{c.key: row[c.key] for c in dataset.columns if row[c.key]} for row in rows]


def json_projection(path, dataset):
    """(line number, `_raw`) of the records of an NDJSON file the dataset selects."""
    out = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        record = json.loads(line)
        column, values = dataset.select
        if record.get(column) not in values:
            continue
        raw = {}
        for c in dataset.columns:
            holder = record if c.within is None else record.get(c.within, {})
            if holder.get(c.key) is None:
                continue
            if c.within is None:
                raw[c.key] = holder[c.key]
            else:
                raw.setdefault(c.within, {})[c.key] = holder[c.key]
        out.append((number, raw))
    return out


@pytest.fixture
def samples(tmp_path):
    """A copy of the sample refresh directory that a test may change."""
    return shutil.copytree(SAMPLES, tmp_path / "refresh")


def assert_nothing_written(con):
    for table in (*BRONZE_TABLES, "bronze_snapshots", "bronze_rejects", "snapshot_sets"):
        present = con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [table]
        ).fetchone()
        if present:
            assert con.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), table
    assert con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE temporary AND table_name LIKE 'stg_real_%'"
    ).fetchone() == (0,)


# --- the sample files -------------------------------------------------------------------


def test_the_sample_files_are_what_the_generator_writes():
    """Edit `tests/real_format.py` and run it; never the files by hand."""
    expected = build_samples()
    written = {
        path.relative_to(SAMPLES).as_posix(): path.read_bytes()
        for path in SAMPLES.rglob("*")
        if path.is_file()
    }
    assert written == expected


def test_the_sample_manifest_records_each_file():
    manifest = load_manifest(SAMPLES)
    for entry in manifest.files.values():
        data = (SAMPLES / entry.path).read_bytes()
        assert entry.size == len(data)
        assert entry.sha256 == hashlib.sha256(data).hexdigest()
    assert (SAMPLES / "gleif/lei2.csv").read_bytes().count(b"\r\n") == len(
        (SAMPLES / "gleif/lei2.csv").read_bytes().splitlines()
    )


def test_a_refresh_id_comes_from_the_manifest_content():
    manifest = load_manifest(SAMPLES)
    assert manifest.refresh_id == load_manifest(SAMPLES).refresh_id
    changed = Manifest.model_validate(
        manifest.model_dump() | {"files": {"x": manifest.files["gleif_rr"]}}
    )
    assert changed.refresh_id != manifest.refresh_id


# --- what is written --------------------------------------------------------------------


def test_csv_records_are_their_declared_projection_hashed_as_canonical_json(con):
    summary = ingest(con)
    snapshot_id = summary.snapshot_set.snapshot_ids["gleif_l1"]
    rows = bronze_rows(con, "bronze_entities", snapshot_id)
    expected = csv_projection(SAMPLES / "gleif/lei2.csv", L1)

    assert len(rows) == len(expected)
    for (record_id, _, row_hash, raw, as_of, confidence, source), want in zip(
        rows, expected, strict=True
    ):
        assert json.loads(raw) == want
        assert row_hash == sha256_hex(want)
        assert record_id == want["LEI"]
        assert (as_of, confidence, source) == (date(2026, 10, 1), 1.0, "gleif")
    assert [row[1] for row in rows] == list(range(1, len(rows) + 1))


def test_awkward_values_survive(con):
    summary = ingest(con)
    raws = {
        record_id: json.loads(raw)
        for record_id, _, _, raw, *_ in bronze_rows(
            con, "bronze_entities", summary.snapshot_set.snapshot_ids["gleif_l1"]
        )
    }
    assert raws[lei("PEMB")]["Entity.LegalName"] == "Pemberwick, Hale & Co. LLP"
    assert raws[lei("PEMB")]["Entity.OtherEntityNames.OtherEntityName.1"] == '"Pemberwick" Advisory'
    assert raws[lei("THAL")]["Entity.LegalName"] == "ΘΑΛΑΣΣΟΒΙΑ ΝΑΥΤΙΛΙΑΚΗ Α.Ε."
    assert raws[lei("SUB")]["Entity.LegalName"] == "Velling Energía S.A."
    assert raws[lei("ORV")]["Entity.LegalJurisdiction"] == "US-DE"
    # An empty cell is not stated.
    assert "Entity.SuccessorEntity.1.SuccessorLEI" not in raws[lei("HOLD")]
    assert raws[lei("DUP")]["Entity.SuccessorEntity.1.SuccessorLEI"] == lei("KOLM")


def test_a_key_of_several_values(con):
    summary = ingest(con)
    ids = [
        row[0]
        for row in bronze_rows(
            con, "bronze_relationships", summary.snapshot_set.snapshot_ids["gleif_rr"]
        )
    ]
    assert ids[0] == f"{lei('MID')}|{lei('HOLD')}|IS_DIRECTLY_CONSOLIDATED_BY"
    assert len(ids) == len(set(ids)) == 13


@pytest.mark.parametrize("dataset", [OS_ENTITIES, OS_SANCTIONS], ids=lambda d: d.name)
def test_json_records_are_their_declared_projection_hashed_as_canonical_json(con, dataset):
    summary = ingest(con)
    rows = bronze_rows(con, dataset.table, summary.snapshot_set.snapshot_ids[dataset.name])
    expected = json_projection(SAMPLES / "opensanctions/entities.ftm.json", dataset)

    assert len(rows) == len(expected)
    for (record_id, row_num, row_hash, raw, as_of, *_), (line, want) in zip(
        rows, expected, strict=True
    ):
        assert json.loads(raw) == want
        assert row_hash == sha256_hex(want)
        assert (record_id, row_num, as_of) == (want["id"], line, date(2026, 10, 1))


def test_json_values_keep_their_types(con):
    summary = ingest(con)
    raws = {
        record_id: json.loads(raw)
        for record_id, _, _, raw, *_ in bronze_rows(
            con, "bronze_entities", summary.snapshot_set.snapshot_ids["os_entities"]
        )
    }
    orvasko = raws["NK-fxOrvasko"]
    assert orvasko["target"] is True
    assert orvasko["properties"]["leiCode"] == [lei("ORV")]
    assert orvasko["properties"]["alias"] == ["Orvasko Logistics", "Орваско Балк Логистикс"]
    assert raws["NK-fxHalvenstrand"]["target"] is False
    assert raws["NK-fxNorthgate"]["properties"]["alias"] == [
        'Northgate "NPT" Trading',
        "Northgate Petroleum Trading\\FZE",
    ]
    # Undeclared properties are not read; a declared one the record lacks is not stated.
    assert "birthDate" not in raws["NK-fxLomanov"]["properties"]
    assert "leiCode" not in raws["NK-fxLomanov"]["properties"]
    # Records of other schemas are not this dataset's.
    assert not {"NK-fxVessel", "addr-fx-1", "NK-fxOwn1", "os-fx-orv-sdn"} & set(raws)


def test_one_file_feeds_several_datasets(con):
    summary = ingest(con)
    ids = summary.snapshot_set.snapshot_ids
    assert ids["os_entities"] != ids["os_sanctions"]
    hashes = con.execute(
        "SELECT DISTINCT content_sha256 FROM bronze_snapshots WHERE dataset LIKE 'os_%'"
    ).fetchall()
    assert hashes == [(load_manifest(SAMPLES).files["opensanctions_entities"].sha256,)]


def test_each_snapshot_records_its_publish(con):
    summary = ingest(con)
    manifest = load_manifest(SAMPLES)
    snapshot_id = summary.snapshot_set.snapshot_ids["gleif_rr"]
    family, published_at, rejects, meta, recorded = con.execute(
        "SELECT family, published_at, reject_count, CAST(source_meta AS VARCHAR), rows_digest"
        " FROM bronze_snapshots WHERE snapshot_id = ?",
        [snapshot_id],
    ).fetchone()
    assert (family, published_at, rejects) == ("real", date(2026, 10, 1), 0)
    assert json.loads(meta) == {
        "file": "gleif_rr",
        "refresh_id": manifest.refresh_id,
        **manifest.files["gleif_rr"].model_dump(mode="json"),
    }
    assert recorded == rows_digest(con, "bronze_relationships", snapshot_id)


def test_ingesting_again_changes_nothing(con):
    first = ingest(con)
    counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone() for t in BRONZE_TABLES}
    again = ingest(con)
    assert again.snapshot_set.snapshot_set_id == first.snapshot_set.snapshot_set_id
    assert not again.snapshot_set.is_new
    assert {d.status for d in again.datasets} == {"already present"}
    assert {t: con.execute(f"SELECT count(*) FROM {t}").fetchone() for t in BRONZE_TABLES} == counts


def test_a_real_set_holds_only_real_datasets(con):
    fixtures = ingest_fixtures(con).snapshot_set
    real = ingest(con).snapshot_set
    assert real.family == "real" and set(real.snapshot_ids) == set(REQUIRED)
    assert not set(real.snapshot_ids.values()) & set(fixtures.snapshot_ids.values())


def test_a_first_real_ingest_needs_every_dataset(con):
    with pytest.raises(IngestError, match="no earlier 'real' snapshot set"):
        ingest(con, datasets=(L1, RR))
    assert_nothing_written(con)


def test_a_later_ingest_of_one_source_inherits_the_rest(con, samples):
    first = ingest(con).snapshot_set
    with (samples / "gleif/rr.csv").open("ab") as f:
        f.write(
            b'"'
            + lei("KOLM").encode()
            + b'","LEI","'
            + lei("HOLD").encode()
            + b'","LEI","IS_DIRECTLY_CONSOLIDATED_BY","ACTIVE"'
            + b',""' * 49
            + b"\r\n"
        )
    restamp(samples)
    later = ingest(con, samples, datasets=(RR,))
    assert later.snapshot_set.base_set_id == first.snapshot_set_id
    assert later.snapshot_set.snapshot_ids["gleif_l1"] == first.snapshot_ids["gleif_l1"]
    assert later.snapshot_set.snapshot_ids["gleif_rr"] != first.snapshot_ids["gleif_rr"]
    assert {d.dataset: d.status for d in later.datasets}["gleif_l1"] == "inherited"


# --- what is refused --------------------------------------------------------------------


def test_a_file_that_is_not_the_downloaded_one_is_refused(con, samples):
    with (samples / "gleif/lei2.csv").open("ab") as f:
        f.write(b"\r\n")
    with pytest.raises(IngestError, match="not the ones the manifest records"):
        ingest(con, samples)
    assert_nothing_written(con)


def test_a_file_missing_a_declared_column_is_refused(con, samples):
    path = samples / "gleif/rr.csv"
    path.write_bytes(
        path.read_bytes().replace(b'"Relationship.RelationshipStatus"', b'"Status"', 1)
    )
    restamp(samples)
    with pytest.raises(IngestError, match=r"lacks \['Relationship.RelationshipStatus'\]"):
        ingest(con, samples)
    assert_nothing_written(con)


def test_a_missing_file_or_manifest_is_refused(con, samples, tmp_path):
    (samples / "gleif/repex.csv").unlink()
    repex = RealDataset(
        "gleif_repex",
        "bronze_repex",
        "gleif",
        "gleif_repex",
        Format.CSV,
        (Column("LEI"),),
        ("LEI",),
    )
    with pytest.raises(IngestError, match="missing"):
        ingest(con, samples, datasets=(*ALL, repex), required=[*REQUIRED, "gleif_repex"])
    with pytest.raises(IngestError, match="no readable manifest"):
        ingest(con, tmp_path)
    with pytest.raises(IngestError, match="not a directory"):
        ingest(con, tmp_path / "absent")
    assert_nothing_written(con)


def test_a_dataset_with_no_records_is_refused(con):
    nothing = RealDataset(
        "os_entities",
        "bronze_entities",
        "opensanctions",
        "opensanctions_entities",
        Format.NDJSON,
        (Column("id"), Column("schema")),
        ("id",),
        select=("schema", frozenset({"Airline"})),
    )
    with pytest.raises(IngestError, match="holds no os_entities records"):
        ingest(con, datasets=(L1, RR, nothing, OS_SANCTIONS))
    assert_nothing_written(con)


# --- rejects ----------------------------------------------------------------------------


@pytest.fixture
def damaged(samples):
    """Samples with records that cannot be loaded, each kind once."""
    lei2 = samples / "gleif/lei2.csv"
    lines = lei2.read_bytes().split(b"\r\n")
    width = lines[0].count(b",")
    blank = b',""' * (width - 1)
    lines[3:3] = [
        b'"FXRBROKEN","Broken "quote" Ltd"' + blank,  # record 3: unreadable
        b'"","No Key Ltd"' + blank,  # record 4: no key
        b'"' + lei("HOLD").encode() + b'","Brackwater Again"' + blank,  # record 5: repeats a key
    ]
    lei2.write_bytes(b"\r\n".join(lines))

    ftm = samples / "opensanctions/entities.ftm.json"
    lines = ftm.read_bytes().split(b"\n")
    lines[2:2] = [
        b'{"id":"NK-broken","schema":"Company"',  # line 3: not JSON
        b"",  # line 4: blank, not a record
        b'["not", "an", "object"]',  # line 5
        b'{"id":"NK-fxBad","schema":"Company","properties":{"name":"one name"}}',  # line 6
        b'{"schema":"Company","properties":{"name":["No Id"]}}',  # line 7
        b'{"id":"NK-fxOrvasko","schema":"Company","properties":{"name":["Again"]}}',  # line 8
    ]
    ftm.write_bytes(b"\n".join(lines))
    restamp(samples)
    return samples


def test_records_that_cannot_be_loaded_are_rejected_and_counted(con, damaged, monkeypatch):
    monkeypatch.setenv(REJECT_LIMIT_ENV_VAR, "0.5")
    summary = ingest(con, damaged)
    ids = summary.snapshot_set.snapshot_ids
    rejects = {
        dataset: con.execute(
            "SELECT row_num, reason FROM bronze_rejects WHERE snapshot_id = ? ORDER BY row_num",
            [ids[dataset]],
        ).fetchall()
        for dataset in ("gleif_l1", "os_entities", "os_sanctions")
    }
    assert [r for _, r in rejects["gleif_l1"]][1:] == [MISSING_KEY, DUPLICATE_KEY]
    assert rejects["gleif_l1"][0][0] == 3 and rejects["gleif_l1"][0][1].startswith(UNREADABLE)
    assert rejects["os_entities"] == [
        (3, f"{UNREADABLE}: not a JSON object"),
        (5, f"{UNREADABLE}: not a JSON object"),
        (6, BAD_VALUE),
        (7, MISSING_KEY),
        (8, DUPLICATE_KEY),
    ]
    # A line that cannot be read may have been any dataset's record.
    assert rejects["os_sanctions"] == rejects["os_entities"][:2]

    counts = {d.dataset: d.reject_count for d in summary.datasets}
    assert counts == {"gleif_l1": 3, "gleif_rr": 0, "os_entities": 5, "os_sanctions": 2}
    # The first record with a key is the one kept, and records keep their place in the file.
    rows = {
        record_id: (row_num, json.loads(raw))
        for record_id, row_num, _, raw, *_ in bronze_rows(con, "bronze_entities", ids["gleif_l1"])
    }
    assert rows[lei("HOLD")] == (1, rows[lei("HOLD")][1])
    assert rows[lei("HOLD")][1]["Entity.LegalName"] == "Brackwater Velling Holdings plc"
    assert rows[lei("SUB")][0] == 6
    assert con.execute(
        "SELECT raw FROM bronze_rejects WHERE snapshot_id = ? AND row_num = 6", [ids["os_entities"]]
    ).fetchone() == ('{"id":"NK-fxBad","schema":"Company","properties":{"name":"one name"}}',)


def test_too_many_rejects_refuse_the_ingest(con, damaged):
    with pytest.raises(IngestError, match=r"3 of 23 gleif_l1 records cannot be loaded"):
        ingest(con, damaged)
    assert_nothing_written(con)


# --- declarations -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"key": ("absent",)}, "key must be declared"),
        ({"key": ()}, "key must be declared"),
        ({"select": ("absent", frozenset({"x"}))}, "select column"),
        ({"columns": (Column("LEI"), Column("LEI"))}, "declared twice"),
        ({"columns": (Column("LEI"), Column("x", within="LEI"))}, "both a value and an object"),
        ({"columns": (Column("LEI"), Column("n", kind=TEXT_LIST))}, "CSV column is top-level"),
    ],
)
def test_a_declaration_is_checked(change, message):
    fields = {
        "name": "d",
        "table": "bronze_entities",
        "source": "gleif",
        "file": "f",
        "format": Format.CSV,
        "columns": (Column("LEI"),),
        "key": ("LEI",),
    }
    with pytest.raises(ValueError, match=message):
        RealDataset(**(fields | change))
