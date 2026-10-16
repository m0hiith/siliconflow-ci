"""Fixtures for the on-target tests. See docs/hil.md, "tests/hil".

Environment:
  SFCI_SERIAL_PORT      the board's VCOM UART (falls back to SFCI_HIL_SERIAL_PORT)
  SFCI_SERIAL_BAUD      default 115200
  SFCI_RESET_CMD        resets the board; default: ci/hil/flash.sh reset
  SFCI_EXPECTED_SHA     the 40-character commit the firmware was built from;
                        default: commit_sha in $SFCI_HIL_BUILD_JSON
                        (default $SFCI_OUT_DIR/hil/build.json, from ci/hil/fetch.py)
  SFCI_OUT_DIR          default out; the UART capture goes to logs/hil-serial.log
                        and the JUnit report to junit-hil.xml under it

Exit codes follow ci/hil (docs/hil.md): pytest's own, except 2 for a missing
setting and 75 when the serial port or the reset failed (infrastructure).
"""
from __future__ import annotations

import os
import shlex
import subprocess
import time

import pytest
import serial

from hil import CONFIG_EXIT, INFRA_EXIT, REPO, Board, abort, exit_override, out_dir
from serial_capture import SerialCapture


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    # Before the junitxml plugin reads it. An explicit --junitxml wins.
    if not config.option.xmlpath:
        config.option.xmlpath = str(out_dir() / "junit-hil.xml")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    code = session.config.stash.get(exit_override, None)
    if code is not None and exitstatus not in (pytest.ExitCode.OK, pytest.ExitCode.INTERRUPTED):
        session.exitstatus = code


@pytest.fixture(scope="session")
def uart(request: pytest.FixtureRequest):
    """The board's UART, open exclusively; every byte received is captured to
    $SFCI_OUT_DIR/logs/hil-serial.log for the whole session."""
    config = request.config
    port = os.environ.get("SFCI_SERIAL_PORT") or os.environ.get("SFCI_HIL_SERIAL_PORT")
    if not port:
        abort(config, CONFIG_EXIT, "set SFCI_SERIAL_PORT to the board's UART, e.g. /dev/ttyACM0")
    try:
        baud = int(os.environ.get("SFCI_SERIAL_BAUD", "115200"))
    except ValueError:
        abort(config, CONFIG_EXIT, f"SFCI_SERIAL_BAUD is not a number: {os.environ['SFCI_SERIAL_BAUD']!r}")

    try:
        # exclusive: fail, rather than share the port with a terminal left open on it.
        ser = serial.Serial(port, baud, timeout=0.1, exclusive=True)
    except (serial.SerialException, OSError) as exc:
        abort(config, INFRA_EXIT, f"infrastructure: cannot open serial port {port}: {exc}")

    log_path = out_dir() / "logs" / "hil-serial.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    capture = SerialCapture(ser, log_path.open("wb"))
    yield capture
    capture.close()
    if capture.error is not None:
        # The port went away mid-run (board unplugged, USB reset).
        abort(config, INFRA_EXIT, f"infrastructure: serial port {port} failed: {capture.error}")


@pytest.fixture(scope="session")
def board(uart: SerialCapture, request: pytest.FixtureRequest) -> Board:
    """The board, reset once for the session with the UART already open, so
    nothing it prints at boot is missed."""
    config = request.config
    reset_cmd = os.environ.get("SFCI_RESET_CMD") or shlex.join([str(REPO / "ci" / "hil" / "flash.sh"), "reset"])
    since_reset = uart.mark()
    reset_at = time.monotonic()
    try:
        result = subprocess.run(shlex.split(reset_cmd), cwd=REPO)
    except OSError as exc:
        abort(config, INFRA_EXIT, f"infrastructure: cannot run reset command {reset_cmd!r}: {exc}")
    if result.returncode == CONFIG_EXIT:  # flash.sh: Commander arguments not filled in, or a setting missing
        abort(config, CONFIG_EXIT, f"reset command {reset_cmd!r}: configuration error (exit 2)")
    if result.returncode != 0:
        abort(config, INFRA_EXIT, f"infrastructure: reset command {reset_cmd!r} exited {result.returncode}")
    return Board(uart=uart, reset_at=reset_at, since_reset=since_reset)
