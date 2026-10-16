"""Optional radio tests. Each is skipped unless its environment is set, since
it needs an access point or a Bluetooth adapter next to the board.

  SFCI_TEST_SSID          Wi-Fi network the board joins (no whitespace)
  SFCI_TEST_PSK           its passphrase, if it has one (sent to the board,
                          never logged)
  SFCI_TEST_WIFI_TIMEOUT_S  default 30
  SFCI_TEST_BLE_NAME      the name the board advertises over BLE
  SFCI_TEST_BLE_TIMEOUT_S   default 15

The firmware does not implement Wi-Fi or BLE yet (docs/hil.md): with these
set, the tests fail and say so, rather than skip, because setting them claims
the firmware under test should pass them.
"""
from __future__ import annotations

import asyncio
import os
import re

import pytest

from hil import Board

SSID = os.environ.get("SFCI_TEST_SSID", "")
PSK = os.environ.get("SFCI_TEST_PSK", "")
BLE_NAME = os.environ.get("SFCI_TEST_BLE_NAME", "")

WIFI_REPLY_RE = r"^SFCI-(?:WIFI (?P<wifi>.*)|CLI (?P<cli>error .*))$"
JOINED_RE = r"joined ssid=(?P<ssid>\S+) ip=(?P<ip>\d{1,3}(?:\.\d{1,3}){3})"


@pytest.mark.skipif(not SSID, reason="set SFCI_TEST_SSID to test joining Wi-Fi")
def test_wifi_join(board: Board):
    """`wifi-join <ssid> [<psk>]` gets `SFCI-WIFI joined ssid=<ssid> ip=<a.b.c.d>`."""
    if re.search(r"\s", SSID + PSK):
        pytest.fail("SFCI_TEST_SSID and SFCI_TEST_PSK cannot contain whitespace (the command is split on spaces)", pytrace=False)
    timeout = float(os.environ.get("SFCI_TEST_WIFI_TIMEOUT_S", "30"))
    board.wait_ready()

    start = board.uart.mark()
    board.uart.send_line(f"wifi-join {SSID} {PSK}".rstrip())
    reply = board.uart.wait_for(WIFI_REPLY_RE, timeout=timeout, since=start)
    assert reply is not None, f"no SFCI-WIFI reply within {timeout:.0f} s"
    _, m = reply
    if m["cli"] == "error unknown-command":
        pytest.fail("this firmware has no wifi-join command (Wi-Fi is not implemented yet)", pytrace=False)
    assert m["cli"] is None, f"the board refused the command: {m['cli']}"
    joined = re.fullmatch(JOINED_RE, m["wifi"])
    assert joined, f"did not join: {m['wifi']}"
    assert joined["ssid"] == SSID


@pytest.mark.skipif(not BLE_NAME, reason="set SFCI_TEST_BLE_NAME to test BLE advertising")
def test_ble_advertising(board: Board):
    """The host sees the board advertising under SFCI_TEST_BLE_NAME."""
    try:
        from bleak import BleakScanner
    except ImportError:
        pytest.fail("bleak is not installed: pip install -r tests/hil/requirements.txt", pytrace=False)
    timeout = float(os.environ.get("SFCI_TEST_BLE_TIMEOUT_S", "15"))
    board.wait_ready()

    device = asyncio.run(BleakScanner.find_device_by_name(BLE_NAME, timeout=timeout))
    assert device is not None, f"no BLE advertisement named {BLE_NAME!r} within {timeout:.0f} s"
