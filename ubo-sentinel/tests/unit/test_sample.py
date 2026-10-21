"""`ubo sample`: a smaller refresh, closed under references, of its own family."""

import csv
import io
import json

import pytest
from e2e import cli
from real_format import SAMPLES, lei

from ubo_sentinel.models.manifest import FAMILY_SAMPLE, load_manifest
from ubo_sentinel.pipeline.bronze import ALIAS_REAL, UnknownSnapshotSet, resolve_set
from ubo_sentinel.pipeline.loaders.real_loader import ingest_all
from ubo_sentinel.pipeline.sample import SampleError, sample

OS_PATH = "opensanctions/entities.ftm.json"


def rows(directory, path):
    text = (directory / path).read_text(encoding="utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def records(directory):
    lines = (directory / OS_PATH).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


@pytest.fixture
def cut(tmp_path):
    return sample(SAMPLES, n=4, seed=7, out_root=tmp_path / "downloads")


def test_a_sample_is_closed_under_references(cut):
    directory = cut.refresh_dir
    leis = {r["LEI"] for r in rows(directory, "gleif/lei2.csv")}
    assert len(leis) >= 4
    for r in rows(directory, "gleif/rr.csv"):
        assert r["Relationship.StartNode.NodeID"] in leis
        assert r["Relationship.EndNode.NodeID"] in leis
    assert {r["LEI"] for r in rows(directory, "gleif/repex.csv")} <= leis
    # Every parent a chosen LEI's relationship names is chosen (when the registry has it).
    published = rows(SAMPLES, "gleif/rr.csv")
    registry = {r["LEI"] for r in rows(SAMPLES, "gleif/lei2.csv")}
    for r in published:
        if (
            r["Relationship.StartNode.NodeID"] in leis
            and r["Relationship.EndNode.NodeID"] in registry
        ):
            assert r["Relationship.EndNode.NodeID"] in leis

    listed = records(directory)
    ids = {r["id"] for r in listed}
    for r in listed:
        props = r.get("properties", {})
        if r["schema"] == "Sanction":
            assert set(props["entity"]) & ids
        if r["schema"] == "Ownership":
            assert {props["owner"][0], props["asset"][0]} <= ids
        # A list record that states an LEI the registry has brings it along.
        for code in props.get("leiCode", []):
            if code in registry:
                assert code in leis


def test_a_sample_keeps_the_published_formats(cut):
    data = (cut.refresh_dir / "gleif/lei2.csv").read_bytes()
    assert data.startswith(b'"LEI","Entity.LegalName"') and b'"\r\n"' in data
    published = set((SAMPLES / OS_PATH).read_bytes().splitlines())
    assert set((cut.refresh_dir / OS_PATH).read_bytes().splitlines()) <= published


def test_a_sample_of_everything_is_the_published_files(tmp_path):
    whole = sample(SAMPLES, n=1000, seed=0, out_root=tmp_path, list_n=1000)
    for path in ("gleif/lei2.csv", "gleif/repex.csv"):
        assert (whole.refresh_dir / path).read_bytes() == (SAMPLES / path).read_bytes(), path
    # Less the relationships whose parent the registry file does not hold.
    registry = {r["LEI"] for r in rows(SAMPLES, "gleif/lei2.csv")}
    kept = [
        r
        for r in rows(SAMPLES, "gleif/rr.csv")
        if {r["Relationship.StartNode.NodeID"], r["Relationship.EndNode.NodeID"]} <= registry
    ]
    assert rows(whole.refresh_dir, "gleif/rr.csv") == kept
    assert len(kept) < len(rows(SAMPLES, "gleif/rr.csv"))
    # Less the unreadable lines and the links the graph does not hold.
    assert len(records(whole.refresh_dir)) < len((SAMPLES / OS_PATH).read_bytes().splitlines())


def test_the_same_seed_gives_the_same_sample(tmp_path, cut):
    again = sample(SAMPLES, n=4, seed=7, out_root=tmp_path / "again")
    assert again.manifest == cut.manifest
    other = sample(SAMPLES, n=4, seed=8, out_root=tmp_path / "again")
    assert other.manifest.refresh_id != cut.manifest.refresh_id
    present = sample(SAMPLES, n=4, seed=7, out_root=tmp_path / "again")
    assert present.status == "already present"


def test_the_manifest_says_it_is_a_sample(cut):
    manifest = load_manifest(cut.refresh_dir)
    source = load_manifest(SAMPLES)
    assert manifest.family == FAMILY_SAMPLE
    assert (manifest.sample.refresh_id, manifest.sample.n, manifest.sample.seed) == (
        source.refresh_id,
        4,
        7,
    )
    for key, entry in manifest.files.items():
        original = source.files[key]
        assert (entry.url, entry.published_at, entry.licence) == (
            original.url,
            original.published_at,
            original.licence,
        )
        assert entry.publisher_checksum is None


def test_a_sample_set_is_never_real(con, cut):
    snapshot_set = ingest_all(con, cut.refresh_dir).snapshot_set
    assert snapshot_set.family == FAMILY_SAMPLE
    with pytest.raises(UnknownSnapshotSet):
        resolve_set(con, ALIAS_REAL)


def test_a_designated_registry_company_brings_its_list_record(tmp_path):
    """Seeded until the designated Orvasko LLC is chosen: its OFAC record comes with it."""
    for seed in range(200):
        cut = sample(SAMPLES, n=1, seed=seed, out_root=tmp_path, list_n=0)
        if {r["LEI"] for r in rows(cut.refresh_dir, "gleif/lei2.csv")} >= {lei("ORV")}:
            break
    else:
        pytest.fail("no seed chose Orvasko")
    listed = records(cut.refresh_dir)
    assert any(r["schema"] == "Sanction" for r in listed)


@pytest.mark.parametrize("n", [0, -1])
def test_a_sample_needs_a_record(tmp_path, n):
    with pytest.raises(SampleError, match="at least one"):
        sample(SAMPLES, n=n, seed=0, out_root=tmp_path)


def test_a_directory_without_a_manifest_cannot_be_sampled(tmp_path):
    with pytest.raises(SampleError, match="no readable manifest"):
        sample(tmp_path, n=1, seed=0, out_root=tmp_path / "out")


def test_the_command(tmp_path):
    result = cli("sample", "--path", SAMPLES, "--n", "3", "--seed", "1", "--out", tmp_path)
    assert result.exit_code == 0, result.output
    assert "(new)" in result.output and "'sample' family" in result.output
    failed = cli("sample", "--path", tmp_path / "none", "--n", "3")
    assert failed.exit_code == 1 and "Sample failed" in failed.output
