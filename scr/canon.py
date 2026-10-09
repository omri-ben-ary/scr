"""Canonical serialization and tagged hashing. See docs/hashing.md."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable

VERSION_PREFIX = "scriptorium/v1/"
ZERO = "0" * 64


def _check(x: Any) -> None:
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        raise ValueError("NaN/Infinity are not canonicalizable")
    if isinstance(x, dict):
        for k, v in x.items():
            if not isinstance(k, str):
                raise ValueError(f"non-string key {k!r}")
            _check(v)
    elif isinstance(x, (list, tuple)):
        for v in x:
            _check(v)


def canon(x: Any) -> str:
    """Canonical JSON: sorted keys, no whitespace, UTF-8 kept, no NaN."""
    _check(x)
    return json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def H(tag: str, *parts: str) -> str:
    """Tagged, versioned hash of string parts."""
    payload = VERSION_PREFIX + tag + "\n" + "\n".join(parts)
    return sha256_text(payload)


def hash_obj(tag: str, x: Any) -> str:
    return H(tag, canon(x))


def merkle_root(leaves: Iterable[str], tag: str = "node") -> str:
    """Pairwise fold; odd leaf duplicated; empty tree hashes to H(tag)."""
    level = list(leaves)
    if not level:
        return H(tag)
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        level = [H(tag, level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]
