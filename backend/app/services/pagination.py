"""Opaque keyset cursors over (created_at, id), newest first."""
import base64
import binascii
import json
from datetime import datetime


class InvalidCursor(ValueError):
    pass


def encode_cursor(created_at: datetime, row_id: str) -> str:
    raw = json.dumps({"c": created_at.isoformat(), "i": row_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        created_at = datetime.fromisoformat(data["c"])
        row_id = data["i"]
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise InvalidCursor("invalid cursor") from exc
    if created_at.tzinfo is None or not isinstance(row_id, str):
        raise InvalidCursor("invalid cursor")
    return created_at, row_id
