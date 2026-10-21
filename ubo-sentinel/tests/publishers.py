"""GLEIF and OpenSanctions as `ubo download` sees them, on a mock transport.

`Publishers` serves the sample refresh directory's files the way the
publishers do: GLEIF's golden-copy API naming a zip per file, OpenSanctions'
`index.json` naming the FtM file with its SHA-1. A test changes what is
published (`publish`) and sees what was requested (`requests`). No socket is
opened.
"""

import hashlib
import io
import zipfile

import httpx
from real_format import build_samples

from ubo_sentinel.download.publishers import GLEIF_LATEST, OPENSANCTIONS_INDEX

GLEIF_BASE = "https://goldencopy.gleif.org/storage/golden-copy-files/"
OS_BASE = "https://data.opensanctions.org/artifacts/sanctions/"

# GLEIF file type -> path in the sample directory.
_GLEIF = {"lei2": "gleif/lei2.csv", "rr": "gleif/rr.csv", "repex": "gleif/repex.csv"}
_OS = "opensanctions/entities.ftm.json"


def zipped(name: str, data: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, data)
    return buffer.getvalue()


class Publishers:
    def __init__(self) -> None:
        self.files = build_samples()
        self.requests: list[httpx.Request] = []
        # Overrides: a wrong SHA-1 in the index; a server that ignores ranges.
        self.os_checksum: str | None = None
        self.ranges = True
        self.publish(gleif="2026-10-01 00:00:00", opensanctions="2026-10-01T06:00:00")

    def publish(
        self,
        gleif: str | None = None,
        opensanctions: str | None = None,
        files: dict[str, bytes] | None = None,
    ):
        """Publish anew: a GLEIF and/or OpenSanctions publish date, and changed file bytes
        (path in the refresh directory -> bytes)."""
        self.files |= files or {}
        if gleif is not None:
            self.gleif_date = gleif
        if opensanctions is not None:
            self.os_date = opensanctions
        stamp = self.gleif_date[:10].replace("-", "/")
        self.blobs = {
            f"{GLEIF_BASE}{stamp}/{kind}-golden-copy.csv.zip": zipped(
                f"{kind}-golden-copy.csv", self.files[path]
            )
            for kind, path in _GLEIF.items()
        }
        version = self.os_date.replace("-", "").replace(":", "").replace("T", "")
        self.os_url = f"{OS_BASE}{version}-fxr/entities.ftm.json"
        self.blobs[self.os_url] = self.files[_OS]

    def gleif_urls(self) -> list[str]:
        return [url for url in self.blobs if url.startswith(GLEIF_BASE)]

    def fetched(self, url: str) -> int:
        """How many times a file was requested."""
        return sum(str(r.url) == url for r in self.requests)

    def _index(self) -> dict:
        data = self.blobs[self.os_url]
        return {
            "name": "sanctions",
            "version": self.os_url.split("/")[-2],
            "updated_at": self.os_date,
            "resources": [
                {"name": "statistics.json", "url": OS_BASE + "statistics.json"},
                {
                    "name": "entities.ftm.json",
                    "url": self.os_url,
                    "size": len(data),
                    "checksum": self.os_checksum or hashlib.sha1(data).hexdigest(),
                },
            ],
        }

    def _latest(self) -> dict:
        publish = {"publish_date": self.gleif_date}
        for kind in _GLEIF:
            url = next(u for u in self.gleif_urls() if f"/{kind}-golden" in u)
            publish[kind] = {
                "type": kind,
                "publish_date": self.gleif_date,
                "full_file": {"csv": {"url": url, "size": len(self.blobs[url])}},
            }
        return {"data": publish}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == GLEIF_LATEST:
            return httpx.Response(200, json=self._latest())
        if url == OPENSANCTIONS_INDEX:
            return httpx.Response(200, json=self._index())
        if url not in self.blobs:
            return httpx.Response(404)
        data = self.blobs[url]
        wanted = request.headers.get("Range")
        if wanted and self.ranges:
            start = int(wanted.removeprefix("bytes=").rstrip("-"))
            if start >= len(data):
                return httpx.Response(416)
            return httpx.Response(206, content=data[start:])
        return httpx.Response(200, content=data)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))
