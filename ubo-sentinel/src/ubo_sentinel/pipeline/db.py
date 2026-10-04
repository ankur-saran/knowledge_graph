"""The DuckDB database file and how to open it."""

import os
from pathlib import Path

import duckdb

# Commands are run from the repository root.
DEFAULT_DB_PATH = Path("var/ubo.duckdb")
# Tests point this at a temporary file.
DB_ENV_VAR = "UBO_DB"


def db_path() -> Path:
    return Path(os.environ.get(DB_ENV_VAR) or DEFAULT_DB_PATH)


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the database. DuckDB allows one writer, so readers pass `read_only=True`."""
    path = db_path()
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path), read_only=read_only)
