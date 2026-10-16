"""tests/hil, run as CI runs it (pytest tests/hil), against a pseudo-terminal
and a fake board that boots when the reset command runs and answers the
firmware's UART commands. Checks the fixtures, the capture log, the JUnit
report and the exit codes; the tests' verdicts on real output are what the
fake board's scripts exercise."""
import fcntl
import os
import select
import subprocess
import sys
import threading
import tty
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytest.importorskip("serial", reason="tests/hil needs pyserial: pip install -r tests/hil/requirements.txt")

REPO = Path(__file__).resolve().parents[2]
COMMIT = "3484a3fb816e0859fd6e1cea078d76385ff50625"
BOOT = "SFCI-BOOT version=v1.4.0-2-g3484a3f sha=3484a3f"
READY = ["SFCI-HIL hooks enabled", "sensor_logic on SiWx917 started"]
# What the real firmware prints for an injected press (see test_sfci_bounce.c).
BOUNCE = ["SFCI-CLI ok inject-bounce", "      1030 ms  button  PRESSED", "      1254 ms  button  RELEASED"]


class FakeBoard:
    """Prints `boot` once the reset command has created `flag`, then answers
    each line it receives with replies[line] (default: unknown-command)."""

    def __init__(self, master: int, flag: Path, boot: list[str], replies: dict[str, list[str]]):
        self.master, self.flag, self.boot, self.replies = master, flag, boot, replies
        self.received: list[str] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def say(self, lines: list[str]) -> None:
        os.write(self.master, "".join(f"{l}\r\n" for l in lines).encode())

    def run(self) -> None:
        while not self.flag.exists():
            if self.stop.wait(0.01):
                return
        self.say(self.boot)
        buf = b""
        while not self.stop.is_set():
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if not ready:
                continue
            try:
                buf += os.read(self.master, 1024)
            except OSError:
                return
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode().rstrip("\r")
                self.received.append(text)
                self.say(self.replies.get(text, ["SFCI-CLI error unknown-command"]))


class Rig:
    """A pseudo-terminal for the port, and a reset command that boots the fake board."""

    def __init__(self, tmp_path: Path):
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)  # a UART does not echo; a cooked pty would, before pyserial opens it
        self.port = os.ttyname(self.slave)
        self.flag = tmp_path / "reset"
        self.boards: list[FakeBoard] = []

    def board(self, boot: list[str], replies: dict[str, list[str]] | None = None) -> "FakeBoard":
        b = FakeBoard(self.master, self.flag, boot, replies or {})
        self.boards.append(b)
        return b

    def env(self, **extra: str) -> dict[str, str]:
        return {"SFCI_SERIAL_PORT": self.port, "SFCI_RESET_CMD": f"touch {self.flag}", **extra}

    def close(self) -> None:
        for b in self.boards:
            b.stop.set()
            b.thread.join(timeout=2)
        os.close(self.master)
        os.close(self.slave)


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def run_hil(tmp_path: Path, env: dict[str, str], *args: str) -> tuple[subprocess.CompletedProcess, Path]:
    out = tmp_path / "out"
    base = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("SFCI_", "PYTEST_"))  # nothing from the developer's shell
    }
    base.update(SFCI_OUT_DIR=str(out), SFCI_EXPECTED_SHA=COMMIT, SFCI_BOOT_SETTLE_S="0.2")
    base.update(env)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/hil", "-p", "no:cacheprovider", "-rA", *args],
        cwd=REPO, env=base, capture_output=True, text=True, timeout=120,
    )
    return proc, out


def junit(out: Path) -> dict[str, str]:
    """{test name: passed|failed|error|skipped} from out/junit-hil.xml."""
    root = ET.parse(out / "junit-hil.xml").getroot()
    results = {}
    for case in root.iter("testcase"):
        kinds = [child.tag for child in case if child.tag in ("failure", "error", "skipped")]
        results[case.get("name")] = {"failure": "failed"}.get(kinds[0], kinds[0]) if kinds else "passed"
    return results


def test_healthy_board_passes_and_radios_skip(rig, tmp_path):
    board = rig.board(["ROM", BOOT, *READY], {"inject-bounce": BOUNCE})
    proc, out = run_hil(tmp_path, rig.env())

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert junit(out) == {
        "test_boot_banner": "passed",
        "test_debounce_on_target": "passed",
        "test_wifi_join": "skipped",
        "test_ble_advertising": "skipped",
    }
    assert board.received == ["inject-bounce"]
    log = (out / "logs" / "hil-serial.log").read_bytes()
    # Every byte as received, CRs included.
    assert log == "".join(f"{l}\r\n" for l in ["ROM", BOOT, *READY, *BOUNCE]).encode()

    props = {p.get("name"): p.get("value") for p in ET.parse(out / "junit-hil.xml").getroot().iter("property")}
    assert props["firmware_sha"] == "3484a3f"
    assert props["firmware_version"] == "v1.4.0-2-g3484a3f"


def test_build_json_supplies_the_commit(rig, tmp_path):
    build = tmp_path / "build.json"
    build.write_text(f'{{"build_id": "bld_01J", "commit_sha": "{COMMIT}"}}')
    rig.board([BOOT, *READY])
    proc, out = run_hil(tmp_path, rig.env(SFCI_EXPECTED_SHA="", SFCI_HIL_BUILD_JSON=str(build)),
                        "-k", "boot_banner")
    assert proc.returncode == 0, proc.stdout


@pytest.mark.parametrize(
    "boot, reason",
    [
        ([], "no SFCI-BOOT line within 10 s"),
        ([BOOT.replace("3484a3f", "deadbee"), *READY], "firmware reports sha=deadbee"),
        ([BOOT, "HardFault", BOOT, *READY], "printed 2 times"),
    ],
)
def test_bad_boot_fails_the_banner_test(rig, tmp_path, boot, reason):
    rig.board(boot)
    proc, out = run_hil(tmp_path, rig.env(), "-k", "boot_banner")
    assert proc.returncode == 1
    assert junit(out)["test_boot_banner"] == "failed"
    assert reason in proc.stdout


@pytest.mark.parametrize(
    "boot, replies, reason",
    [
        # Debouncer let a bounce through.
        ([BOOT, *READY], {"inject-bounce": [*BOUNCE[:2], "      1032 ms  button  RELEASED",
                                            "      1050 ms  button  PRESSED", BOUNCE[2]]},
         "got ['PRESSED', 'RELEASED', 'PRESSED', 'RELEASED']"),
        ([BOOT, *READY], {"inject-bounce": BOUNCE[:2]}, "never reported RELEASED"),
        ([BOOT, *READY], {"inject-bounce": [BOUNCE[0], "      1030 ms  button  PRESSED", "      1100 ms  button  RELEASED"]},
         "took 70 ms"),
        ([BOOT, *READY], {"inject-bounce": ["SFCI-CLI error busy"]}, "error busy"),
        ([BOOT, *READY], {"inject-bounce": []}, "no SFCI-CLI reply"),
        ([BOOT, *READY], {"inject-bounce": [*BOUNCE, BOOT]}, "reset during the test"),
        ([BOOT, READY[1]], {}, "build it with SFCI_HIL_HOOKS=1"),
        ([BOOT], {}, "did not print 'sensor_logic on SiWx917 started'"),
    ],
)
def test_bad_debounce_fails(rig, tmp_path, boot, replies, reason):
    rig.board(boot, replies)
    proc, out = run_hil(tmp_path, rig.env(), "-k", "debounce")
    assert proc.returncode == 1, proc.stdout
    assert junit(out)["test_debounce_on_target"] == "failed"
    assert reason in proc.stdout


def test_wifi_join_and_the_passphrase_stays_out_of_the_log(rig, tmp_path):
    board = rig.board([BOOT, *READY], {"wifi-join lab-ap s3cret-psk": ["SFCI-WIFI joined ssid=lab-ap ip=192.168.4.20"]})
    proc, out = run_hil(tmp_path, rig.env(SFCI_TEST_SSID="lab-ap", SFCI_TEST_PSK="s3cret-psk"),
                        "-k", "wifi")
    assert proc.returncode == 0, proc.stdout
    assert board.received == ["wifi-join lab-ap s3cret-psk"]
    assert b"s3cret-psk" not in (out / "logs" / "hil-serial.log").read_bytes()
    assert "s3cret-psk" not in proc.stdout + (out / "junit-hil.xml").read_text()


def test_wifi_on_firmware_without_it_fails_and_says_why(rig, tmp_path):
    rig.board([BOOT, *READY])
    proc, out = run_hil(tmp_path, rig.env(SFCI_TEST_SSID="lab-ap"), "-k", "wifi")
    assert proc.returncode == 1
    assert "no wifi-join command" in proc.stdout


def test_missing_port_is_infra(tmp_path):
    proc, out = run_hil(tmp_path, {"SFCI_SERIAL_PORT": str(tmp_path / "ttyACM9"), "SFCI_RESET_CMD": "true"})
    assert proc.returncode == 75, proc.stdout
    assert set(junit(out).values()) == {"error", "skipped"}
    assert "infrastructure: cannot open serial port" in proc.stdout


def test_busy_port_is_infra(rig, tmp_path):
    holder = os.open(rig.port, os.O_RDWR | os.O_NOCTTY)
    fcntl.flock(holder, fcntl.LOCK_EX)  # as a terminal left open with pyserial would
    try:
        proc, _ = run_hil(tmp_path, rig.env())
    finally:
        os.close(holder)
    assert proc.returncode == 75, proc.stdout


def test_failed_reset_is_infra(rig, tmp_path):
    proc, _ = run_hil(tmp_path, rig.env(SFCI_RESET_CMD="sh -c 'exit 75'"))
    assert proc.returncode == 75
    assert "reset command" in proc.stdout


def test_unconfigured_reset_is_a_config_error(rig, tmp_path):
    proc, _ = run_hil(tmp_path, rig.env(SFCI_RESET_CMD="sh -c 'exit 2'"))
    assert proc.returncode == 2


def test_no_port_setting_is_a_config_error(tmp_path):
    proc, out = run_hil(tmp_path, {})
    assert proc.returncode == 2
    assert "set SFCI_SERIAL_PORT" in proc.stdout
    assert (out / "junit-hil.xml").exists()


def test_old_port_variable_still_works(rig, tmp_path):
    rig.board([BOOT, *READY])
    env = rig.env()
    env["SFCI_HIL_SERIAL_PORT"] = env.pop("SFCI_SERIAL_PORT")
    proc, _ = run_hil(tmp_path, env, "-k", "boot_banner")
    assert proc.returncode == 0, proc.stdout
