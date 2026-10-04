"""Canonical JSON and hashing.

Everything that hashes or compares payloads byte-for-byte goes through here.
"""

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

# Percentages and exposure values are kept to 4 decimal places (DuckDB DECIMAL(7,4)).
DECIMAL_QUANTUM = Decimal("0.0001")


def quantize(value: Decimal) -> Decimal:
    return value.quantize(DECIMAL_QUANTUM)


def _normalise(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return _normalise(obj.model_dump())
    if isinstance(obj, Decimal):
        return str(quantize(obj))
    if isinstance(obj, date | datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(key): _normalise(value) for key, value in obj.items()}
    if isinstance(obj, list | tuple):
        return [_normalise(value) for value in obj]
    if obj is None or isinstance(obj, str | int | float | bool):
        return obj
    raise TypeError(f"Cannot serialise {type(obj).__name__} to canonical JSON")


def canonical_json(obj: Any) -> bytes:
    """Sorted keys, no whitespace, UTF-8. `Decimal` is written as a 4-place string."""
    return json.dumps(
        _normalise(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def short_id(parts: list[Any]) -> str:
    """Deterministic 16-hex-character id from a list of parts."""
    return sha256_hex(parts)[:16]
