"""Pure-Python units: IDs, cursors, JSON logging."""
import io
import json
import logging
import re
from datetime import datetime, timezone

import pytest

from app.ids import new_id, ulid
from app.logs import JsonFormatter
from app.services.pagination import InvalidCursor, decode_cursor, encode_cursor

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def test_ulid_format_and_time_ordering():
    a = ulid(now_ms=1_000)
    b = ulid(now_ms=2_000)
    assert ULID_RE.match(a) and ULID_RE.match(b)
    assert a < b  # lexicographic order follows the timestamp


def test_new_id_prefixes():
    assert re.match(r"^bld_[0-9A-Z]{26}$", new_id("bld"))
    assert new_id("art").startswith("art_")
    with pytest.raises(ValueError):
        new_id("xyz")


def test_cursor_round_trip_keeps_microseconds():
    ts = datetime(2026, 10, 14, 9, 30, 1, 123456, tzinfo=timezone.utc)
    assert decode_cursor(encode_cursor(ts, "bld_X")) == (ts, "bld_X")


@pytest.mark.parametrize(
    "bad",
    ["", "%%%", "bm90IGpzb24", "eyJjIjoiMjAyNi0xMC0xNFQwOTozMDowMSJ9", "eyJjIjoibm9wZSIsImkiOiJ4In0"],
)
def test_cursor_rejects_garbage(bad):
    # empty / not base64 / not JSON / missing "i" / unparsable datetime
    with pytest.raises(InvalidCursor):
        decode_cursor(bad)


def test_json_formatter_emits_one_object_with_extras():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("sfci.test.json")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info("build created", extra={"build_id": "bld_1", "number": 7})
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logger.exception("failed")

    first, second = (json.loads(line) for line in stream.getvalue().splitlines() if line.startswith("{"))
    assert first["msg"] == "build created"
    assert first["level"] == "info"
    assert first["logger"] == "sfci.test.json"
    assert first["build_id"] == "bld_1" and first["number"] == 7
    assert first["ts"].endswith("Z")
    assert "RuntimeError: boom" in second["exc"]
