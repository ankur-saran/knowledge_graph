"""`ubo download`: fetch the latest publishes into a refresh directory with its manifest.

Layout: `<root>/<refresh_id>/{gleif,opensanctions}/...` and `manifest.json`,
where `<root>` is `var/downloads`. `refresh_id` comes from the manifest's
content, so the same publishes give the same directory and fetching them
again changes nothing. A file an earlier refresh already holds (same URL, and
the same checksum when the publisher states one) is linked from it, not
fetched: GLEIF's relationship files change less often than the list.

Files are fetched into `<root>/.partial/`, where an interrupted fetch resumes.
A file whose checksum is not the publisher's is deleted and the download
fails. A file smaller than the last refresh's by more than the shrink limit is
refused unless the fall is accepted; it stays in `.partial/`, so accepting it
does not fetch it again. The directory appears whole or not at all.
"""

import hashlib
import json
import os
import shutil
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ubo_sentinel.download import http
from ubo_sentinel.download.publishers import PUBLISHERS, Planned, PublisherError
from ubo_sentinel.models.canonical import short_id
from ubo_sentinel.models.manifest import MANIFEST_NAME, Manifest, ManifestFile, load_manifest
from ubo_sentinel.pipeline.bronze import file_sha256
from ubo_sentinel.pipeline.quality import shrink_limit

PARTIAL = ".partial"
# A fetched file's size and hashes, beside it in `.partial/` once it is complete.
_DONE = ".done.json"


class DownloadError(RuntimeError):
    """The download failed; no refresh directory was written."""


class ShrinkError(DownloadError):
    """A file is much smaller than the last refresh's; accept the fall to keep it."""

    def __init__(self, shrunk: dict[str, tuple[int, int]]) -> None:
        self.shrunk = shrunk
        detail = "; ".join(
            f"{key}: {new:,} bytes, was {old:,}" for key, (old, new) in shrunk.items()
        )
        super().__init__(
            f"Files more than {shrink_limit():.0%} smaller than the last refresh's: {detail}."
            " A truncated file would look like mass delistings; pass --accept-shrink"
            " if the fall is real."
        )


@dataclass(frozen=True)
class DownloadResult:
    refresh_dir: Path
    manifest: Manifest
    # "new" or "already downloaded".
    status: str
    # File key -> "fetched", "resumed" or the refresh id it was linked from.
    origins: dict[str, str] = field(default_factory=dict)
    # File key -> (last refresh's size, this one's), for files accepted although they shrank.
    shrunk: dict[str, tuple[int, int]] = field(default_factory=dict)


def _earlier(root: Path) -> list[tuple[Path, Manifest]]:
    """The refresh directories under `root` that hold the publishers' files (not samples)."""
    found = []
    if not root.is_dir():
        return found
    for directory in sorted(root.iterdir()):
        if directory.name.startswith(".") or not (directory / MANIFEST_NAME).is_file():
            continue
        try:
            manifest = load_manifest(directory)
        except (OSError, ValueError):
            continue
        if manifest.sample is None:
            found.append((directory, manifest))
    return found


def _reusable(
    planned: Planned, earlier: list[tuple[Path, Manifest]]
) -> tuple[Path, ManifestFile, str] | None:
    """A file of an earlier refresh that is this publish's file, still intact."""
    for directory, manifest in earlier:
        entry = manifest.files.get(planned.key)
        if entry is None or entry.url != planned.url:
            continue
        if planned.publisher_checksum and entry.publisher_checksum != planned.publisher_checksum:
            continue
        path = directory / entry.path
        if (
            path.is_file()
            and path.stat().st_size == entry.size
            and file_sha256(path) == entry.sha256
        ):
            return path, entry, manifest.refresh_id
    return None


def _unzip(archive: Path, target: Path) -> tuple[int, str]:
    """Extract the one file of a GLEIF zip. Returns its size and SHA-256."""
    try:
        with zipfile.ZipFile(archive) as z:
            members = [m for m in z.infolist() if not m.is_dir()]
            if len(members) != 1:
                raise DownloadError(f"{archive.name}: holds {len(members)} files, not one.")
            digest = hashlib.sha256()
            with z.open(members[0]) as src, target.open("wb") as dst:
                while chunk := src.read(http.CHUNK):
                    dst.write(chunk)
                    digest.update(chunk)
    except zipfile.BadZipFile as exc:
        archive.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise DownloadError(f"{archive.name}: not a readable zip ({exc}); deleted.") from exc
    return target.stat().st_size, digest.hexdigest()


def _fetch(client: httpx.Client, planned: Planned, partial: Path) -> tuple[Path, ManifestFile, str]:
    """Fetch one file into `.partial/` (or find it there, complete), checked."""
    stem = f"{planned.key}-{short_id([planned.url])}"
    staged = partial / stem
    done = partial / (stem + _DONE)
    if staged.is_file() and done.is_file():
        info = json.loads(done.read_text(encoding="utf-8"))
        origin = "resumed"
    else:
        part = partial / (stem + (".zip" if planned.zipped else "") + ".part")
        origin = "resumed" if part.exists() else "fetched"
        fetched = http.fetch(client, planned.url, part)
        if planned.publisher_checksum is not None:
            algorithm, expected = planned.publisher_checksum.split(":", 1)
            if algorithm != "sha1":
                raise PublisherError(f"{planned.url}: checksum algorithm '{algorithm}' unknown.")
            if fetched.sha1 != expected:
                part.unlink(missing_ok=True)
                raise DownloadError(
                    f"{planned.url}: SHA-1 {fetched.sha1} is not the publisher's {expected};"
                    " the file was deleted."
                )
        if planned.zipped:
            size, sha256 = _unzip(part, staged)
            info = {"size": size, "sha256": sha256, "archive_sha256": fetched.sha256}
            done.write_text(json.dumps(info), encoding="utf-8")
            part.unlink()
        else:
            os.replace(part, staged)
            info = {"size": fetched.size, "sha256": fetched.sha256, "archive_sha256": None}
            done.write_text(json.dumps(info), encoding="utf-8")
    entry = ManifestFile(
        path=planned.path,
        source=planned.source,
        url=planned.url,
        published_at=planned.published_at,
        size=info["size"],
        sha256=info["sha256"],
        publisher_checksum=planned.publisher_checksum,
        archive_sha256=info["archive_sha256"],
        licence=planned.licence,
    )
    return staged, entry, origin


def _shrunk(manifest: Manifest, earlier: list[tuple[Path, Manifest]]) -> dict[str, tuple[int, int]]:
    """Files smaller than the same file of the newest earlier publish by more than the limit."""
    limit = shrink_limit()
    shrunk = {}
    for key, entry in manifest.files.items():
        before = [
            m.files[key]
            for _, m in earlier
            if key in m.files and m.files[key].published_at < entry.published_at
        ]
        if not before:
            continue
        last = max(before, key=lambda e: e.published_at)
        if entry.size < last.size * (1 - limit):
            shrunk[key] = (last.size, entry.size)
    return shrunk


def _place(source: Path, target: Path, link: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if not link:
        os.replace(source, target)
        return
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def download(
    root: Path,
    client: httpx.Client | None = None,
    accept_shrink: bool = False,
    say: Callable[[str], None] = lambda _: None,
) -> DownloadResult:
    """Fetch the latest publishes into `root/<refresh_id>/`."""
    own_client = client is None
    client = client or http.http_client()
    try:
        planned = [p for publisher in PUBLISHERS.values() for p in publisher(client)]
        earlier = _earlier(root)
        partial = root / PARTIAL
        partial.mkdir(parents=True, exist_ok=True)

        files: dict[str, tuple[Path, ManifestFile, bool]] = {}
        origins: dict[str, str] = {}
        for p in planned:
            reused = _reusable(p, earlier)
            if reused is not None:
                path, entry, refresh_id = reused
                files[p.key] = (path, entry, True)
                origins[p.key] = refresh_id
                say(f"  {p.key:<24} unchanged; linked from {refresh_id}")
                continue
            say(f"  {p.key:<24} fetching {p.url}")
            path, entry, origin = _fetch(client, p, partial)
            files[p.key] = (path, entry, False)
            origins[p.key] = origin
    except httpx.HTTPError as exc:
        raise DownloadError(f"Download failed: {exc}") from exc
    finally:
        if own_client:
            client.close()

    manifest = Manifest(files={key: files[key][1] for key in sorted(files)})
    shrunk = _shrunk(manifest, earlier)
    if shrunk and not accept_shrink:
        raise ShrinkError(shrunk)

    target = root / manifest.refresh_id
    if (target / MANIFEST_NAME).is_file():
        status = "already downloaded"
    else:
        building = root / f".building-{manifest.refresh_id}"
        shutil.rmtree(building, ignore_errors=True)
        for path, entry, link in files.values():
            _place(path, building / entry.path, link)
        manifest.write(building)
        os.replace(building, target)
        status = "new"
    for path, _, link in files.values():
        if not link:
            path.unlink(missing_ok=True)
            (path.parent / (path.name + _DONE)).unlink(missing_ok=True)
    return DownloadResult(target, manifest, status, origins, shrunk)
