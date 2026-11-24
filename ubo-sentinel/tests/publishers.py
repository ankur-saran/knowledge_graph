"""GLEIF, OpenSanctions and Companies House as `ubo download` sees them, on a mock transport.

`Publishers` serves the sample refresh directory's files the way the
publishers do: GLEIF's golden-copy API naming a zip per file, OpenSanctions'
`index.json` naming the FtM file with its SHA-1, Companies House's two download
pages listing each file in dated parts (two here), each a zip of one file, the
company file's parts each with its header row. A test changes what is
published (`publish`) and sees what was requested (`requests`). No socket is
opened.
"""

import hashlib
import io
import zipfile

import httpx
from real_format import build_samples

from ubo_sentinel.download.publishers import (
    COMPANIES_HOUSE_BASE,
    COMPANIES_HOUSE_FILES,
    GLEIF_LATEST,
    OPENSANCTIONS_INDEX,
)

GLEIF_BASE = "https://goldencopy.gleif.org/storage/golden-copy-files/"
OS_BASE = "https://data.opensanctions.org/artifacts/sanctions/"

# GLEIF file type -> path in the sample directory.
_GLEIF = {"lei2": "gleif/lei2.csv", "rr": "gleif/rr.csv", "repex": "gleif/repex.csv"}
_OS = "opensanctions/entities.ftm.json"
_CH_COMPANIES = "companies_house/companies.csv"
_CH_PSC = "companies_house/psc.txt"


def split_parts(data: bytes, parts: int, header: bool) -> list[bytes]:
    """A file cut into `parts` at line ends, each part with the header row when `header`."""
    lines = data.splitlines(keepends=True)
    head, body = (lines[:1], lines[1:]) if header else ([], lines)
    size = -(-len(body) // parts)
    return [b"".join(head + body[i * size : (i + 1) * size]) for i in range(parts)]


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
        self.publish(
            gleif="2026-10-01 00:00:00",
            opensanctions="2026-10-01T06:00:00",
            companies_house="2026-10-01",
        )

    def publish(
        self,
        gleif: str | None = None,
        opensanctions: str | None = None,
        files: dict[str, bytes] | None = None,
        companies_house: str | None = None,
    ):
        """Publish anew: a GLEIF, OpenSanctions and/or Companies House publish date, and
        changed file bytes (path in the refresh directory -> bytes)."""
        self.files |= files or {}
        if gleif is not None:
            self.gleif_date = gleif
        if opensanctions is not None:
            self.os_date = opensanctions
        if companies_house is not None:
            self.ch_date = companies_house
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
        self.ch_parts: dict[str, list[str]] = {}
        names = {
            "ch_companies": lambda n, m: f"BasicCompanyData-{self.ch_date}-part{n}_{m}.zip",
            "ch_psc": lambda n, m: f"psc-snapshot-{self.ch_date}_{n}of{m}.zip",
        }
        for key, path in (("ch_companies", _CH_COMPANIES), ("ch_psc", _CH_PSC)):
            header = COMPANIES_HOUSE_FILES[key][3]
            parts = split_parts(self.files[path], 2, header)
            urls = []
            for n, data in enumerate(parts, start=1):
                name = names[key](n, len(parts))
                url = COMPANIES_HOUSE_BASE + name
                self.blobs[url] = zipped(name.replace(".zip", ".csv" if header else ".txt"), data)
                urls.append(url)
            self.ch_parts[key] = urls

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

    def _page(self, key: str) -> str:
        """A download page as Companies House writes it: links to each part, and others."""
        links = "".join(
            f'<li><a href="{url.removeprefix(COMPANIES_HOUSE_BASE)}">'
            f"{url.removeprefix(COMPANIES_HOUSE_BASE)} (1Mb)</a></li>"
            for url in self.ch_parts[key]
        )
        return (
            "<html><body><h1>Download</h1><ul>"
            f'<li><a href="BasicCompanyDataAsOneFile-{self.ch_date}.zip">One file</a></li>'
            f"{links}</ul></body></html>"
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == GLEIF_LATEST:
            return httpx.Response(200, json=self._latest())
        if url == OPENSANCTIONS_INDEX:
            return httpx.Response(200, json=self._index())
        for key, (page, *_rest) in COMPANIES_HOUSE_FILES.items():
            if url == page:
                return httpx.Response(200, text=self._page(key))
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
