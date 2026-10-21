"""`ubo download`: the publishers' files into a refresh directory, checked, on a mock transport."""

import hashlib
import io
import json
import zipfile
from datetime import datetime

import httpx
import pytest
from e2e import cli
from publishers import Publishers
from real_format import build_samples

from ubo_sentinel.download import DownloadError, ShrinkError, download
from ubo_sentinel.download.publishers import PublisherError
from ubo_sentinel.download.refresh import PARTIAL
from ubo_sentinel.models.canonical import short_id
from ubo_sentinel.models.manifest import FAMILY_REAL, load_manifest
from ubo_sentinel.pipeline.loaders.real_loader import ingest_all

SAMPLES = build_samples()
OS_PATH = "opensanctions/entities.ftm.json"


@pytest.fixture
def root(tmp_path):
    return tmp_path / "downloads"


def fetch(root, served, **kwargs):
    with served.client() as client:
        return download(root, client=client, **kwargs)


def test_a_refresh_holds_the_published_files_and_says_where_they_came_from(root, con):
    served = Publishers()
    result = fetch(root, served)
    assert result.status == "new"
    assert result.refresh_dir == root / result.manifest.refresh_id
    for path in ("gleif/lei2.csv", "gleif/rr.csv", "gleif/repex.csv", OS_PATH):
        assert (result.refresh_dir / path).read_bytes() == SAMPLES[path]
    manifest = load_manifest(result.refresh_dir)
    assert manifest == result.manifest and manifest.family == FAMILY_REAL

    lei2 = manifest.files["gleif_lei2"]
    url = next(u for u in served.gleif_urls() if "/lei2-" in u)
    assert (lei2.url, lei2.source, lei2.licence) == (url, "gleif", "CC0-1.0")
    assert lei2.published_at == datetime(2026, 10, 1)
    # GLEIF states no checksum; the zip's hash is kept.
    assert lei2.publisher_checksum is None
    assert lei2.archive_sha256 == hashlib.sha256(served.blobs[url]).hexdigest()
    listed = manifest.files["opensanctions_entities"]
    assert listed.publisher_checksum == "sha1:" + hashlib.sha1(SAMPLES[OS_PATH]).hexdigest()
    assert (listed.licence, listed.archive_sha256) == ("CC-BY-NC-4.0", None)
    assert listed.published_at == datetime(2026, 10, 1, 6)
    # Nothing is left half-done.
    assert list((root / PARTIAL).iterdir()) == []
    # The loaders read it.
    assert ingest_all(con, result.refresh_dir).snapshot_set.family == FAMILY_REAL


def test_the_same_publishes_give_the_same_directory_and_are_not_fetched_again(root):
    served = Publishers()
    first = fetch(root, served)
    again = fetch(root, served)
    assert (again.status, again.refresh_dir) == ("already downloaded", first.refresh_dir)
    assert set(again.origins.values()) == {first.manifest.refresh_id}
    for url in served.blobs:
        assert served.fetched(url) == 1


def test_a_new_list_is_fetched_and_the_unchanged_registry_linked(root):
    served = Publishers()
    first = fetch(root, served)
    served.publish(
        opensanctions="2026-10-02T06:00:00",
        files={OS_PATH: SAMPLES[OS_PATH] + b"\n"},
    )
    later = fetch(root, served)
    assert later.refresh_dir != first.refresh_dir
    assert later.origins["opensanctions_entities"] == "fetched"
    for key in ("gleif_lei2", "gleif_rr", "gleif_repex"):
        assert later.origins[key] == first.manifest.refresh_id
        assert later.manifest.files[key] == first.manifest.files[key]
    assert all(served.fetched(url) == 1 for url in served.gleif_urls())


def test_a_file_whose_checksum_is_not_the_publishers_is_deleted(root):
    served = Publishers()
    served.os_checksum = "0" * 40
    with pytest.raises(DownloadError, match="is not the publisher's"):
        fetch(root, served)
    assert [p.name for p in root.iterdir()] == [PARTIAL]
    assert not any(p.name.startswith("opensanctions") for p in (root / PARTIAL).iterdir())


def test_a_file_that_shrank_is_refused_unless_the_fall_is_accepted(root):
    served = Publishers()
    first = fetch(root, served)
    lines = SAMPLES[OS_PATH].splitlines(keepends=True)
    served.publish(
        opensanctions="2026-10-02T06:00:00",
        files={OS_PATH: b"".join(lines[:3])},
    )
    with pytest.raises(ShrinkError, match="opensanctions_entities") as refused:
        fetch(root, served)
    old, new = refused.value.shrunk["opensanctions_entities"]
    assert new < 0.8 * old
    assert sorted(p.name for p in root.iterdir()) == sorted([PARTIAL, first.refresh_dir.name])

    accepted = fetch(root, served, accept_shrink=True)
    assert accepted.status == "new"
    assert accepted.shrunk == {"opensanctions_entities": (old, new)}
    # The refused file was kept, so accepting it did not fetch it again.
    assert served.fetched(served.os_url) == 1


def test_the_shrink_limit_can_be_set(root, monkeypatch):
    served = Publishers()
    fetch(root, served)
    served.publish(
        opensanctions="2026-10-02T06:00:00",
        files={OS_PATH: SAMPLES[OS_PATH][: len(SAMPLES[OS_PATH]) * 9 // 10]},
    )
    monkeypatch.setenv("UBO_SHRINK_LIMIT", "0.05")
    with pytest.raises(ShrinkError):
        fetch(root, served)
    monkeypatch.setenv("UBO_SHRINK_LIMIT", "0.5")
    assert fetch(root, served).status == "new"


def partial_os_file(root, served):
    """Half of the list file, as an interrupted fetch leaves it."""
    data = served.blobs[served.os_url]
    stem = f"opensanctions_entities-{short_id([served.os_url])}"
    part = root / PARTIAL / f"{stem}.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(data[: len(data) // 2])
    return data


@pytest.mark.parametrize("ranges", [True, False])
def test_an_interrupted_fetch_resumes(root, ranges):
    served = Publishers()
    served.ranges = ranges
    data = partial_os_file(root, served)
    result = fetch(root, served)
    assert (result.refresh_dir / OS_PATH).read_bytes() == data
    assert result.origins["opensanctions_entities"] == "resumed"
    (request,) = [r for r in served.requests if str(r.url) == served.os_url]
    assert request.headers["Range"] == f"bytes={len(data) // 2}-"


def test_a_zip_that_cannot_be_read_fails_the_download(root):
    served = Publishers()
    url = served.gleif_urls()[0]
    served.blobs[url] = b"not a zip"
    with pytest.raises(DownloadError, match="not a readable zip"):
        fetch(root, served)


def test_a_zip_of_several_files_fails_the_download(root):
    served = Publishers()
    url = served.gleif_urls()[0]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("a.csv", b"x")
        z.writestr("b.csv", b"y")
    served.blobs[url] = buffer.getvalue()
    with pytest.raises(DownloadError, match="holds 2 files"):
        fetch(root, served)


def test_an_index_this_code_cannot_read_is_refused(root):
    served = Publishers()

    def broken(request):
        if "index.json" in str(request.url):
            return httpx.Response(200, json={"resources": []})
        return served.handler(request)

    with httpx.Client(transport=httpx.MockTransport(broken)) as client:
        with pytest.raises(PublisherError, match="unexpected content"):
            download(root, client=client)


def test_a_publisher_that_is_down_fails_the_download(root):
    def down(request):
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(down)) as client:
        with pytest.raises(DownloadError, match="503"):
            download(root, client=client)


def test_the_command(publishers):
    result = cli("download")
    assert result.exit_code == 0, result.output
    assert "(new)" in result.output and "CC-BY-NC-4.0" in result.output
    again = cli("download")
    assert "(already downloaded)" in again.output and "unchanged; linked from" in again.output

    publishers.os_checksum = "0" * 40
    publishers.publish(opensanctions="2026-10-03T06:00:00")
    failed = cli("download")
    assert failed.exit_code == 1 and "Download failed" in failed.output


def test_a_refresh_directory_records_its_manifest_readably(root):
    result = fetch(root, Publishers())
    text = (result.refresh_dir / "manifest.json").read_text(encoding="utf-8")
    assert text.startswith("{\n  ") and json.loads(text)["files"]
