#!/usr/bin/env python3
"""Open the board's UART for the hardware-in-the-loop test, exclusively.

    python3 ci/hil/serial_port.py check <port>

`check` opens the port the way the test will and exits 0, or 75 when it is
missing or busy, so ci/hil/flash.sh can fail fast before flashing.

Standard library only (termios), so the runner needs no pyserial. A port is
busy when another process holds it exclusively (TIOCEXCL, as screen and
pyserial set it) or holds its flock (pyserial's `exclusive=True`).
"""
import errno
import fcntl
import os
import sys
import termios

INFRA_EXIT = 75  # sysexits EX_TEMPFAIL: the classifier's infra_hw

BAUD_RATES = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
}


class PortError(Exception):
    """The port is missing, busy or unusable: infrastructure, not firmware."""


def open_exclusive(path: str, baud: int = 115200) -> int:
    """Open `path` raw at `baud`, 8N1, non-blocking, and hold it exclusively.
    Returns the file descriptor. Raises PortError."""
    if baud not in BAUD_RATES:
        raise ValueError(f"unsupported baud rate {baud}")
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except FileNotFoundError:
        raise PortError(f"serial port {path} not found (board unplugged?)") from None
    except OSError as exc:
        if exc.errno in (errno.EBUSY, errno.EACCES, errno.EPERM):
            raise PortError(f"serial port {path} busy or not permitted: {exc.strerror}") from None
        raise PortError(f"cannot open serial port {path}: {exc.strerror}") from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise PortError(f"serial port {path} busy: another process holds its lock") from None
        try:
            fcntl.ioctl(fd, termios.TIOCEXCL)
            attrs = termios.tcgetattr(fd)
        except OSError as exc:
            raise PortError(f"{path} is not a usable serial port: {exc.strerror}") from None
        cflag, cc = attrs[2], attrs[6]
        speed = BAUD_RATES[baud]
        attrs = [
            0,  # iflag: no input processing
            0,  # oflag: no output processing
            (cflag & ~(termios.CSIZE | termios.PARENB | termios.CSTOPB))
            | termios.CS8 | termios.CREAD | termios.CLOCAL,  # 8N1, receiver on
            0,  # lflag: raw, no echo
            speed,
            speed,
            cc,
        ]
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        return fd
    except BaseException:
        os.close(fd)
        raise


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "check":
        print("usage: serial_port.py check <port>", file=sys.stderr)
        return 2
    try:
        os.close(open_exclusive(argv[1]))
    except PortError as exc:
        print(f"serial_port.py: {exc}", file=sys.stderr)
        return INFRA_EXIT
    print(f"serial port {argv[1]} is free")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
