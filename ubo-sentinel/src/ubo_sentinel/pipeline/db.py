"""The DuckDB database file and how to open it."""

import os
from pathlib import Path

import duckdb

# Commands are run from the repository root.
DEFAULT_DB_PATH = Path("var/ubo.duckdb")
# Tests point this at a temporary file.
DB_ENV_VAR = "UBO_DB"

# Resource limits for large builds. Unset, DuckDB's own defaults apply (memory:
# 80 % of RAM; threads: one per core). A writer spills to `UBO_DUCKDB_TMP`
# (default: `tmp/` beside the database) instead of failing when the limit is reached.
MEMORY_ENV_VAR = "UBO_DUCKDB_MEMORY"
THREADS_ENV_VAR = "UBO_DUCKDB_THREADS"
TMP_ENV_VAR = "UBO_DUCKDB_TMP"


def db_path() -> Path:
    return Path(os.environ.get(DB_ENV_VAR) or DEFAULT_DB_PATH)


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the database. DuckDB allows one writer, so readers pass `read_only=True`."""
    path = db_path()
    config: dict[str, str] = {}
    if memory := os.environ.get(MEMORY_ENV_VAR):
        config["memory_limit"] = memory
    if threads := os.environ.get(THREADS_ENV_VAR):
        config["threads"] = threads
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
        spill = Path(os.environ.get(TMP_ENV_VAR) or path.parent / "tmp")
        spill.mkdir(parents=True, exist_ok=True)
        config["temp_directory"] = str(spill)
    return duckdb.connect(str(path), read_only=read_only, config=config)
