"""Deterministic serialization and hashing helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> bytes:
    """Serialize *value* as deterministic, compact UTF-8 JSON."""
    serialized = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return serialized.encode("utf-8")


def hash_bytes(value: bytes) -> str:
    """Return a prefixed SHA-256 digest for *value*."""
    return "sha256:" + hashlib.sha256(value).hexdigest()
