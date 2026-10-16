#!/usr/bin/env python3
"""The hardware-in-the-loop test: reset the board and check its boot line.

    python3 ci/hil/boot_check.py --build out/hil/build.json

Opens the UART first (so nothing printed at boot is missed), resets the board
with `ci/hil/flash.sh reset`, and reads until the firmware prints

    SFCI-BOOT version=<FW_VERSION> sha=<FW_COMMIT_SHORT>

It then keeps reading for --settle seconds. The test passes when exactly one
boot line arrived and its sha is a prefix of the build's commit (from the
build.json ci/hil/fetch.py wrote). A second boot line means the board reset
again, which is a firmware failure too.

Environment: SFCI_HIL_SERIAL_PORT (required unless --port), SFCI_HIL_BAUD
(default 115200).

Exit codes:
  0   passed
  1   the firmware failed: no boot line, a wrong sha, or more than one boot
  2   usage or configuration error
  75  infrastructure: the serial port is missing or busy, or the reset failed
"""
import argparse
import json
import os
import re
import select
import shlex
import subprocess
import sys
import termios
import time
from pathlib import Path

from serial_port import INFRA_EXIT, PortError, open_exclusive

SCRIPT_DIR = Path(__file__).resolve().parent
BOOT_MARK = "SFCI-BOOT"
BOOT_RE = re.compile(r"SFCI-BOOT version=(?P<version>\S+) sha=(?P<sha>\S+)")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHORT_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def verdict(lines: list[str], commit_sha: str) -> tuple[bool, str]:
    """Whether the UART lines show one clean boot of the build's commit. Pure."""
    # The ROM bootloader may leave a partial line; the mark can follow it.
    boots = [line[line.index(BOOT_MARK):] for line in lines if BOOT_MARK in line]
    if not boots:
        return False, "no SFCI-BOOT line: the firmware did not boot, or its UART is not on this port"
    if len(boots) > 1:
        return False, f"SFCI-BOOT printed {len(boots)} times: the board reset again after booting"
    m = BOOT_RE.fullmatch(boots[0].rstrip())
    if m is None:
        return False, f"malformed boot line: {boots[0]!r}"
    sha = m["sha"]
    if not SHORT_SHA_RE.fullmatch(sha) or not commit_sha.startswith(sha):
        return False, f"firmware reports sha={sha}, but the build is commit {commit_sha}"
    return True, f"booted version={m['version']} sha={sha}"


def read_lines(fd: int, timeout_s: float, settle_s: float) -> list[str]:
    """Lines read from fd until settle_s after the first boot line, or until
    timeout_s with none. Each line is echoed to stdout as it arrives."""
    buf = b""
    lines: list[str] = []
    deadline = time.monotonic() + timeout_s
    booted = False

    def take(line: bytes) -> None:
        nonlocal booted, deadline
        text = line.decode("utf-8", errors="replace").rstrip("\r")
        lines.append(text)
        print(f"uart| {text}", flush=True)
        if BOOT_MARK in text and not booted:
            booted = True
            deadline = time.monotonic() + settle_s

    while (left := deadline - time.monotonic()) > 0:
        ready, _, _ = select.select([fd], [], [], min(left, 0.2))
        if not ready:
            continue
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            continue
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            take(line)
    if buf:
        take(buf)
    return lines


def load_commit(path: Path) -> str:
    build = json.loads(path.read_text())
    sha = build.get("commit_sha") if isinstance(build, dict) else None
    if not isinstance(sha, str) or not SHA1_RE.fullmatch(sha):
        raise ValueError(f"{path}: no commit_sha")
    return sha


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--build", type=Path, required=True, help="build.json from ci/hil/fetch.py")
    p.add_argument("--port", default=os.environ.get("SFCI_HIL_SERIAL_PORT"))
    p.add_argument("--baud", type=int, default=int(os.environ.get("SFCI_HIL_BAUD", "115200")))
    p.add_argument("--timeout", type=float, default=15.0, help="seconds from reset to the boot line")
    p.add_argument("--settle", type=float, default=3.0, help="seconds to keep reading after it")
    p.add_argument(
        "--reset-cmd",
        default=shlex.join([str(SCRIPT_DIR / "flash.sh"), "reset"]),
        help="command that resets the board",
    )
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args.port:
        print("boot_check.py: set SFCI_HIL_SERIAL_PORT (or pass --port)", file=sys.stderr)
        return 2
    try:
        commit_sha = load_commit(args.build)
    except (OSError, ValueError) as exc:
        print(f"boot_check.py: {exc}", file=sys.stderr)
        return 2

    try:
        fd = open_exclusive(args.port, args.baud)
    except PortError as exc:
        print(f"boot_check.py: {exc}", file=sys.stderr)
        return INFRA_EXIT
    except ValueError as exc:
        print(f"boot_check.py: {exc}", file=sys.stderr)
        return 2
    try:
        termios.tcflush(fd, termios.TCIFLUSH)  # nothing from before the reset
        reset = subprocess.run(shlex.split(args.reset_cmd))
        if reset.returncode != 0:
            print(f"boot_check.py: reset failed (exit {reset.returncode})", file=sys.stderr)
            return reset.returncode
        lines = read_lines(fd, args.timeout, args.settle)
    finally:
        os.close(fd)

    ok, message = verdict(lines, commit_sha)
    print(f"boot_check.py: {'PASS' if ok else 'FAIL'}: {message}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
