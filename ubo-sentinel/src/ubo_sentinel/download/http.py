"""HTTP: one client, and a fetch that resumes and hashes as it writes."""

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from ubo_sentinel import __version__

# Bytes read and written at a time.
CHUNK = 1024 * 1024
# Attempts at a file when the connection drops; each resumes where the last stopped.
ATTEMPTS = 4


def http_client() -> httpx.Client:
    """The client `ubo download` uses. Tests replace it with one on a mock transport."""
    return httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, read=300.0),
        headers={"User-Agent": f"ubo-sentinel/{__version__}"},
    )


@dataclass(frozen=True)
class Fetched:
    path: Path
    size: int
    sha256: str
    sha1: str


def _hash_existing(path: Path, hashes: list) -> int:
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            for h in hashes:
                h.update(chunk)
    return path.stat().st_size


def fetch(client: httpx.Client, url: str, part: Path, pause: float = 2.0) -> Fetched:
    """Fetch `url` into `part`, resuming a partial file left by an earlier attempt or run.

    Returns the file's size and hashes. A server that ignores the range sends
    the whole file again, and the partial file is replaced.
    """
    part.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, ATTEMPTS + 1):
        sha256, sha1 = hashlib.sha256(), hashlib.sha1()
        have = _hash_existing(part, [sha256, sha1]) if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with client.stream("GET", url, headers=headers) as response:
                if response.status_code == 416 and have:
                    # The partial file is the whole file.
                    return Fetched(part, have, sha256.hexdigest(), sha1.hexdigest())
                response.raise_for_status()
                mode = "ab" if response.status_code == 206 else "wb"
                if mode == "wb":
                    sha256, sha1 = hashlib.sha256(), hashlib.sha1()
                with part.open(mode) as f:
                    for chunk in response.iter_bytes(CHUNK):
                        f.write(chunk)
                        sha256.update(chunk)
                        sha1.update(chunk)
            return Fetched(part, part.stat().st_size, sha256.hexdigest(), sha1.hexdigest())
        except httpx.TransportError:
            if attempt == ATTEMPTS:
                raise
            time.sleep(pause * attempt)
    raise AssertionError("unreachable")
