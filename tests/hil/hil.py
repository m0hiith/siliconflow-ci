"""What the tests in tests/hil share: settings, exit codes and the Board."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from serial_capture import SerialCapture

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "ci" / "hil"))
from boot_check import BOOT_MARK  # noqa: E402  the boot line's contract lives there

CONFIG_EXIT = 2
INFRA_EXIT = 75

# The last line app_init prints: the main loop runs from here on.
READY_LINE = "sensor_logic on SiWx917 started"
READY_TIMEOUT_S = 10.0

exit_override = pytest.StashKey[int]()


def out_dir() -> Path:
    return Path(os.environ.get("SFCI_OUT_DIR", "out"))


def abort(config: pytest.Config, code: int, message: str):
    """Fail the current test (or fixture), and make the run exit with `code`."""
    config.stash.setdefault(exit_override, code)
    pytest.fail(message, pytrace=False)


@dataclass
class Board:
    uart: SerialCapture
    reset_at: float  # time.monotonic() just before the reset command ran
    since_reset: int  # uart.mark() taken then: lines before it predate the reset

    def wait_ready(self) -> None:
        """Wait until the firmware has finished booting."""
        if self.uart.wait_for(f"^{READY_LINE}$", timeout=READY_TIMEOUT_S, since=self.since_reset) is None:
            pytest.fail(f"the board did not print {READY_LINE!r} within {READY_TIMEOUT_S:.0f} s of the reset", pytrace=False)

    def resets_since(self, since: int) -> int:
        return sum(BOOT_MARK in line.text for line in self.uart.lines(since))
