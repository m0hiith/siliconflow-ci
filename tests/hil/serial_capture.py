"""Read the board's UART on a background thread.

Every byte received is written to the log file the moment it arrives, so the
log is complete even if a test hangs or the runner is killed. Complete lines
are also kept in memory, with their arrival time, for tests to wait on.

Bytes sent to the board are not logged: a command may carry a secret (the
Wi-Fi passphrase), and the firmware does not echo.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import BinaryIO

import serial


@dataclass(frozen=True)
class Line:
    at: float  # time.monotonic() when its newline arrived
    text: str


class SerialCapture:
    def __init__(self, port: serial.Serial, log: BinaryIO):
        self._port = port
        self._log = log
        self._lines: list[Line] = []
        self._partial = b""
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self.error: Exception | None = None  # why reading stopped early, if it did
        self._thread = threading.Thread(target=self._run, name="hil-serial", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                chunk = self._port.read(self._port.in_waiting or 1)  # returns after the port timeout
                if chunk:
                    self._log.write(chunk)
                    self._log.flush()
                    self._take(chunk)
        except (serial.SerialException, OSError) as exc:
            self.error = exc
        finally:
            with self._cond:
                if self._partial:
                    self._append(self._partial)
                    self._partial = b""
                self._cond.notify_all()

    def _take(self, chunk: bytes) -> None:
        with self._cond:
            *complete, self._partial = (self._partial + chunk).split(b"\n")
            for raw in complete:
                self._append(raw)
            if complete:
                self._cond.notify_all()

    def _append(self, raw: bytes) -> None:
        self._lines.append(Line(time.monotonic(), raw.decode("utf-8", errors="replace").rstrip("\r")))

    # ------------------------------------------------------------ for tests

    def mark(self) -> int:
        """An index to pass as `since`: only lines that arrive after now."""
        with self._cond:
            return len(self._lines)

    def lines(self, since: int = 0) -> list[Line]:
        with self._cond:
            return self._lines[since:]

    def wait_for(self, pattern: str | re.Pattern[str], timeout: float, since: int = 0) -> tuple[Line, re.Match[str]] | None:
        """The first line from `since` on that `pattern` searches successfully,
        waiting up to `timeout` seconds for it; None if none arrives."""
        regex = re.compile(pattern)
        deadline = time.monotonic() + timeout
        i = since
        with self._cond:
            while True:
                for i in range(i, len(self._lines)):
                    if m := regex.search(self._lines[i].text):
                        return self._lines[i], m
                i = len(self._lines)
                left = deadline - time.monotonic()
                if left <= 0 or not self._thread.is_alive():
                    return None
                self._cond.wait(left)

    def send_line(self, text: str) -> None:
        """Send `text` and a newline to the board. Not logged."""
        self._port.write(text.encode() + b"\n")
        self._port.flush()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._port.close()
        self._log.close()
