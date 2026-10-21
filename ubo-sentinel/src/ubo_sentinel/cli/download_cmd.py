"""`ubo download`: fetch the latest GLEIF and OpenSanctions files into a refresh directory."""

from pathlib import Path
from typing import Annotated

import typer

from ubo_sentinel.audit.log import var_dir
from ubo_sentinel.cli.app import app, require_role
from ubo_sentinel.download import DownloadError, DownloadResult, download
from ubo_sentinel.download.publishers import PublisherError


def downloads_root() -> Path:
    return var_dir() / "downloads"


def run_download(accept_shrink: bool) -> DownloadResult:
    """Download, printing progress; exits 1 on failure."""
    root = downloads_root()
    typer.echo(f"Downloading the latest publishes into {root.as_posix()}/")
    try:
        result = download(root, accept_shrink=accept_shrink, say=typer.echo)
    except (DownloadError, PublisherError) as exc:
        typer.echo(f"Download failed, no refresh directory was written: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    for key, (old, new) in result.shrunk.items():
        typer.echo(f"  {key}: accepted although smaller ({new:,} bytes, was {old:,})")
    typer.echo(f"Refresh: {result.refresh_dir.as_posix()} ({result.status})")
    for key, entry in result.manifest.files.items():
        typer.echo(
            f"  {key:<24} {entry.size:>14,} bytes  published {entry.published_at:%Y-%m-%d %H:%M}"
            f"  {entry.licence}"
        )
    return result


@app.command(name="download")
def download_command(
    accept_shrink: Annotated[
        bool,
        typer.Option("--accept-shrink", help="Keep files much smaller than the last refresh's."),
    ] = False,
    role: Annotated[str, typer.Option("--role")] = "engineer",
) -> None:
    """Fetch the latest GLEIF golden copy and OpenSanctions sanctions collection."""
    require_role(role, "download")
    run_download(accept_shrink)
