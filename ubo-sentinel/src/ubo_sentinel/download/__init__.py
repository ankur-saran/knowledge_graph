"""Fetching the publishers' files: the only code that opens the network.

`ubo download` reads where GLEIF and OpenSanctions publish their latest files,
fetches them and writes a refresh directory with its manifest. Nothing else
imports this package; the loaders read the directory it writes.
"""

from ubo_sentinel.download.refresh import DownloadError, DownloadResult, ShrinkError, download

__all__ = ["DownloadError", "DownloadResult", "ShrinkError", "download"]
