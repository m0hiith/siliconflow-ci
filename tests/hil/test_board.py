"""On-target tests that need only the board and its UART."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest

from hil import CONFIG_EXIT, Board, abort, out_dir
from boot_check import BOOT_MARK, BOOT_RE, SHA1_RE, load_commit, verdict  # isort: skip (hil puts ci/hil on the path)

BOOT_TIMEOUT_S = 10.0
# Read this long after the boot line: a second one means the board reset again.
BOOT_SETTLE_S = float(os.environ.get("SFCI_BOOT_SETTLE_S", "1.0"))

CLI_REPLY_TIMEOUT_S = 2.0
BUTTON_RE = r"^\s*(?P<ms>\d+) ms  button  (?P<event>PRESSED|RELEASED)$"
RELEASED_RE = r"^\s*\d+ ms  button  RELEASED$"
# firmware/src/sfci_bounce.c, debounced at app.c's 20 ms: PRESSED 30 ms and
# RELEASED 254 ms after the script starts (firmware/tests/test_sfci_bounce.c),
# so 224 ms apart. The loop on the target samples less evenly than the unit
# test does, and printing a line blocks it for a few ms.
INJECTED_PRESS_MS = 224
INJECTED_PRESS_TOLERANCE_MS = 20
BOUNCE_SCRIPT_S = 0.334


def expected_commit(config: pytest.Config) -> str:
    sha = os.environ.get("SFCI_EXPECTED_SHA")
    if sha:
        if not SHA1_RE.fullmatch(sha):
            abort(config, CONFIG_EXIT, f"SFCI_EXPECTED_SHA must be a full 40-character commit sha, not {sha!r}")
        return sha
    path = Path(os.environ.get("SFCI_HIL_BUILD_JSON") or out_dir() / "hil" / "build.json")
    try:
        return load_commit(path)
    except (OSError, ValueError) as exc:
        abort(config, CONFIG_EXIT, f"set SFCI_EXPECTED_SHA, or SFCI_HIL_BUILD_JSON to a build.json with commit_sha: {exc}")


def test_boot_banner(board: Board, pytestconfig, record_testsuite_property):
    """The board prints its SFCI-BOOT line within 10 s of a reset, once, with
    the sha of the commit that was built."""
    commit = expected_commit(pytestconfig)
    found = board.uart.wait_for(BOOT_MARK, timeout=BOOT_TIMEOUT_S, since=board.since_reset)
    assert found is not None, f"no {BOOT_MARK} line within {BOOT_TIMEOUT_S:.0f} s of the reset"
    line, _ = found
    took = line.at - board.reset_at
    assert took <= BOOT_TIMEOUT_S, f"{BOOT_MARK} line came {took:.1f} s after the reset"

    time.sleep(BOOT_SETTLE_S)
    ok, message = verdict([l.text for l in board.uart.lines(board.since_reset)], commit)
    assert ok, message

    m = BOOT_RE.search(line.text)
    record_testsuite_property("firmware_version", m["version"])
    record_testsuite_property("firmware_sha", m["sha"])
    record_testsuite_property("boot_s", f"{took:.3f}")


def test_debounce_on_target(board: Board):
    """A bouncy press injected through the HAL test hook comes out of the
    on-target debouncer as exactly one PRESSED and one RELEASED."""
    board.wait_ready()
    if not any(l.text == "SFCI-HIL hooks enabled" for l in board.uart.lines(board.since_reset)):
        pytest.fail("this firmware has no test hooks: build it with SFCI_HIL_HOOKS=1 (ci/siwx917.sh)", pytrace=False)

    start = board.uart.mark()
    board.uart.send_line("inject-bounce")
    reply = board.uart.wait_for(r"^SFCI-CLI (?P<reply>.*)$", timeout=CLI_REPLY_TIMEOUT_S, since=start)
    assert reply is not None, f"no SFCI-CLI reply to inject-bounce within {CLI_REPLY_TIMEOUT_S:.0f} s"
    ack, m = reply
    assert m["reply"] == "ok inject-bounce"

    assert board.uart.wait_for(RELEASED_RE, timeout=CLI_REPLY_TIMEOUT_S, since=start), "the debouncer never reported RELEASED"
    # Then make sure nothing more follows: wait out the script, with margin.
    time.sleep(max(0.0, ack.at + BOUNCE_SCRIPT_S + 0.5 - time.monotonic()))

    lines = board.uart.lines(start)
    assert board.resets_since(start) == 0, "the board reset during the test"
    events = [m for l in lines if (m := re.match(BUTTON_RE, l.text))]
    seen = [e["event"] for e in events]
    assert seen == ["PRESSED", "RELEASED"], f"expected one PRESSED then one RELEASED, got {seen}"
    held = int(events[1]["ms"]) - int(events[0]["ms"])
    assert abs(held - INJECTED_PRESS_MS) <= INJECTED_PRESS_TOLERANCE_MS, (
        f"PRESSED to RELEASED took {held} ms; the injected press debounces to {INJECTED_PRESS_MS} ms"
    )
