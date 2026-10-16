"""Prefixed ULID identifiers, e.g. 'bld_01J9Z3K8Q4W5E6R7T8Y9U0I1O2'.

A ULID is a 48-bit millisecond timestamp followed by 80 random bits, encoded as
26 Crockford base32 characters, so IDs sort roughly by creation time.
"""
import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

PREFIXES = frozenset({"bld", "art", "prj", "rep", "usr"})


def ulid(now_ms: int | None = None) -> str:
    ts = int(time.time() * 1000) if now_ms is None else now_ms
    if not 0 <= ts < (1 << 48):
        raise ValueError("timestamp out of ULID range")
    value = (ts << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(value >> (5 * i)) & 0x1F] for i in range(25, -1, -1))


def new_id(prefix: str) -> str:
    if prefix not in PREFIXES:
        raise ValueError(f"unknown id prefix: {prefix}")
    return f"{prefix}_{ulid()}"
