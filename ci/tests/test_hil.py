"""ci/hil: the boot check against a pseudo-terminal standing in for the
board's UART, flash.sh and retry_infra.sh with a fake Simplicity Commander,
and fetch.py against a local server playing SiliconFlow."""
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

HIL_DIR = Path(__file__).resolve().parents[1] / "hil"
FETCH = HIL_DIR / "fetch.py"
FLASH = HIL_DIR / "flash.sh"
RETRY = HIL_DIR / "retry_infra.sh"

sys.path.insert(0, str(HIL_DIR))
import boot_check  # noqa: E402
import serial_port  # noqa: E402

COMMIT = "3484a3fb816e0859fd6e1cea078d76385ff50625"
BOOT = "SFCI-BOOT version=v1.4.0-2-g3484a3f sha=3484a3f"


# ------------------------------------------------------------- verdict


@pytest.mark.parametrize(
    "lines",
    [
        [BOOT],
        ["", "ROM v1.2", BOOT, "sensor_logic on SiWx917 started"],
        ["\x00\x7f" + BOOT],  # bootloader noise without a newline before it
        ["SFCI-BOOT version=v1.4.0 sha=" + COMMIT],  # a full sha is a prefix too
    ],
)
def test_one_boot_of_the_build_commit_passes(lines):
    ok, message = boot_check.verdict(lines, COMMIT)
    assert ok, message
    assert "sha=3484a3f" in message


@pytest.mark.parametrize(
    "lines, reason",
    [
        ([], "no SFCI-BOOT line"),
        (["sensor_logic on SiWx917 started"], "no SFCI-BOOT line"),
        ([BOOT, "HardFault", BOOT], "printed 2 times"),
        (["SFCI-BOOT version=v1.4.0 sha=deadbee"], "firmware reports sha=deadbee"),
        (["SFCI-BOOT version=v1.4.0 sha=unknown"], "sha=unknown"),
        (["SFCI-BOOT version=v1.4.0 sha=348"], "sha=348"),  # too short to identify a commit
        (["SFCI-BOOT version=v1.4.0"], "malformed"),
        (["SFCI-BOOT version=v1.4.0 sha=3484a3f extra"], "malformed"),
    ],
)
def test_bad_boots_fail(lines, reason):
    ok, message = boot_check.verdict(lines, COMMIT)
    assert not ok
    assert reason in message


# ----------------------------------------------------------- the UART


@pytest.fixture
def uart(tmp_path):
    """A pseudo-terminal: the script opens `port`; the test writes to `master`
    what the board would print."""
    master, slave = os.openpty()
    port = os.ttyname(slave)
    yield master, port
    os.close(master)
    os.close(slave)


@pytest.fixture
def build_json(tmp_path) -> Path:
    path = tmp_path / "build.json"
    path.write_text(json.dumps({"build_id": "bld_01J", "commit_sha": COMMIT}))
    return path


def board(master: int, flag: Path, output: str):
    """Print `output` once the reset command has run (it creates `flag`)."""

    def run():
        deadline = time.monotonic() + 5
        while not flag.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        os.write(master, output.encode())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def check(port, build_json, reset_cmd, timeout="3", settle="0.5"):
    return boot_check.main(
        ["--build", str(build_json), "--port", port, "--timeout", timeout, "--settle", settle, "--reset-cmd", reset_cmd]
    )


def test_boot_check_resets_then_reads_the_boot_line(uart, tmp_path, build_json, capsys):
    master, port = uart
    flag = tmp_path / "reset"
    # Printed before the reset: must not count.
    os.write(master, f"{BOOT}\r\n".encode())
    board(master, flag, f"ROM\r\n{BOOT}\r\nsensor_logic on SiWx917 started\r\n")

    assert check(port, build_json, f"touch {flag}") == 0
    out = capsys.readouterr().out
    assert f"uart| {BOOT}" in out
    assert "PASS: booted version=v1.4.0-2-g3484a3f sha=3484a3f" in out


def test_boot_check_fails_when_the_board_boots_twice(uart, tmp_path, build_json, capsys):
    master, port = uart
    flag = tmp_path / "reset"
    board(master, flag, f"{BOOT}\r\nHardFault\r\n{BOOT}\r\n")
    assert check(port, build_json, f"touch {flag}") == 1
    assert "printed 2 times" in capsys.readouterr().out


def test_boot_check_fails_on_silence(uart, tmp_path, build_json, capsys):
    _, port = uart
    assert check(port, build_json, "true", timeout="0.3") == 1
    assert "no SFCI-BOOT line" in capsys.readouterr().out


def test_missing_port_is_infra(tmp_path, build_json):
    assert check(str(tmp_path / "ttyACM9"), build_json, "true") == serial_port.INFRA_EXIT


def test_busy_port_is_infra(uart, build_json):
    _, port = uart
    holder = os.open(port, os.O_RDWR | os.O_NOCTTY)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        assert check(port, build_json, "true") == serial_port.INFRA_EXIT
    finally:
        os.close(holder)


def test_failed_reset_keeps_its_exit_code(uart, build_json):
    _, port = uart
    assert check(port, build_json, "sh -c 'exit 75'") == 75


def test_boot_check_needs_a_port_and_a_commit(tmp_path, build_json, monkeypatch):
    monkeypatch.delenv("SFCI_HIL_SERIAL_PORT", raising=False)
    assert boot_check.main(["--build", str(build_json)]) == 2
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"build_id": "bld_01J"}))
    assert boot_check.main(["--build", str(bad), "--port", "/dev/null"]) == 2


def test_serial_port_check(uart, tmp_path):
    _, port = uart
    assert serial_port.main(["check", port]) == 0
    assert serial_port.main(["check", str(tmp_path / "nope")]) == 75
    assert serial_port.main([]) == 2


# ------------------------------------------------------- retry_infra.sh


def counting_command(tmp_path: Path, codes: list[int]) -> list[str]:
    """A command that exits with codes[n] on its n-th run, and logs each run."""
    script = tmp_path / "cmd.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'n=$(wc -l < "{tmp_path}/runs" 2>/dev/null || echo 0)\n'
        f'echo run >> "{tmp_path}/runs"\n'
        f"codes=({' '.join(map(str, codes))})\n"
        'exit "${codes[$n]}"\n'
    )
    script.chmod(0o755)
    return [str(script)]


def retry(tmp_path, codes):
    env = dict(os.environ, SFCI_HIL_RETRY_DELAY_S="0")
    r = subprocess.run(["bash", str(RETRY), *counting_command(tmp_path, codes)], env=env, capture_output=True, text=True)
    runs = (tmp_path / "runs").read_text().count("run")
    return r.returncode, runs


@pytest.mark.parametrize(
    "codes, expected",
    [
        ([0], (0, 1)),
        ([1], (1, 1)),  # not infrastructure: never retried
        ([75, 0], (0, 2)),
        ([75, 75, 0], (75, 2)),  # once only
        ([75, 1], (1, 2)),
    ],
)
def test_retry_infra_retries_only_75_and_only_once(tmp_path, codes, expected):
    assert retry(tmp_path, codes) == expected


# ------------------------------------------------------------- flash.sh

TODO_BLOCK = re.compile(r"# >>> TODO\(hil\).*?# <<< TODO\(hil\)[^\n]*\n", re.S)


def fake_commander(tmp_path: Path, fail: str | None = None) -> Path:
    """Logs its arguments; exits 1 when its first argument is `fail`."""
    path = tmp_path / "commander"
    path.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{tmp_path}/commander.log"\n'
        f'[[ "$1" == "{fail}" ]] && {{ echo "ERROR: {fail} failed"; exit 1; }}\n'
        "exit 0\n"
    )
    path.chmod(0o755)
    return path


def filled_flash(tmp_path: Path) -> Path:
    """flash.sh with its TODO block filled, as the user will fill it."""
    text = FLASH.read_text()
    assert len(TODO_BLOCK.findall(text)) == 1
    filled = TODO_BLOCK.sub(
        'probe_args=(probe --device "$device")\n'
        'flash_args=(flash "$image" --device "$device")\n'
        'verify_args=(verify "$image" --device "$device")\n'
        'reset_args=(reset --device "$device")\n',
        text,
    )
    path = tmp_path / "hil" / "flash.sh"
    path.parent.mkdir()
    path.write_text(filled)
    (path.parent / "serial_port.py").write_text((HIL_DIR / "serial_port.py").read_text())
    return path


def run_flash(script: Path, tmp_path: Path, *args, **env_extra):
    env = {**os.environ, "SFCI_HIL_DEVICE": "SiWG917M111MGTBA", **env_extra}
    return subprocess.run(["bash", str(script), *args], env=env, capture_output=True, text=True)


@pytest.fixture
def image(tmp_path) -> Path:
    path = tmp_path / "siwx917_soc.rps"
    path.write_bytes(b"\x00rps image")
    return path


def commander_calls(tmp_path):
    log = tmp_path / "commander.log"
    return log.read_text().splitlines() if log.exists() else []


def test_flash_runs_probe_flash_verify_reset(tmp_path, uart, image):
    _, port = uart
    script = filled_flash(tmp_path)
    r = run_flash(
        script, tmp_path, "flash", str(image), SFCI_HIL_SERIAL_PORT=port, SFCI_HIL_COMMANDER=str(fake_commander(tmp_path))
    )
    assert r.returncode == 0, r.stderr
    assert commander_calls(tmp_path) == [
        "probe --device SiWG917M111MGTBA",
        f"flash {image} --device SiWG917M111MGTBA",
        f"verify {image} --device SiWG917M111MGTBA",
        "reset --device SiWG917M111MGTBA",
    ]


@pytest.mark.parametrize(
    "fail, message", [("probe", "probe not found"), ("flash", "flash failed"), ("verify", "flash verify failed")]
)
def test_flash_failures_are_infra(tmp_path, uart, image, fail, message):
    _, port = uart
    script = filled_flash(tmp_path)
    r = run_flash(
        script, tmp_path, "flash", str(image),
        SFCI_HIL_SERIAL_PORT=port, SFCI_HIL_COMMANDER=str(fake_commander(tmp_path, fail)),
    )
    assert r.returncode == 75
    assert message in r.stderr


def test_flash_checks_the_serial_port_before_touching_the_board(tmp_path, image):
    script = filled_flash(tmp_path)
    r = run_flash(
        script, tmp_path, "flash", str(image),
        SFCI_HIL_SERIAL_PORT=str(tmp_path / "ttyACM9"), SFCI_HIL_COMMANDER=str(fake_commander(tmp_path)),
    )
    assert r.returncode == 75
    assert "not found" in r.stderr
    assert commander_calls(tmp_path) == []


def test_reset_only_resets(tmp_path):
    script = filled_flash(tmp_path)
    r = run_flash(
        script, tmp_path, "reset",
        SFCI_HIL_SERIAL_PORT="/dev/unused", SFCI_HIL_COMMANDER=str(fake_commander(tmp_path, "reset")),
    )
    assert r.returncode == 75 and "reset failed" in r.stderr
    assert commander_calls(tmp_path) == ["reset --device SiWG917M111MGTBA"]


def test_unfilled_todo_is_a_configuration_error(tmp_path, uart, image):
    _, port = uart
    r = run_flash(FLASH, tmp_path, "flash", str(image), SFCI_HIL_SERIAL_PORT=port, SFCI_HIL_COMMANDER=str(fake_commander(tmp_path)))
    assert r.returncode == 2
    assert "TODO: fill probe_args" in r.stderr
    assert commander_calls(tmp_path) == []


@pytest.mark.parametrize(
    "args, env, message",
    [
        (["flash"], {}, "usage"),
        (["erase"], {}, "usage"),
        (["flash", "/nonexistent.rps"], {}, "no such image"),
        (["reset"], {"SFCI_HIL_DEVICE": ""}, "SFCI_HIL_DEVICE is not set"),
        (["reset"], {"SFCI_HIL_SERIAL_PORT": ""}, "SFCI_HIL_SERIAL_PORT is not set"),
    ],
)
def test_flash_usage_errors(tmp_path, args, env, message):
    env = {"SFCI_HIL_SERIAL_PORT": "/dev/unused", "SFCI_HIL_COMMANDER": str(fake_commander(tmp_path)), **env}
    r = run_flash(FLASH, tmp_path, *args, **env)
    assert r.returncode == 2
    assert message in r.stderr


# ------------------------------------------------------------- fetch.py

BUILD_ID = "bld_01JA6Z9D5V8R3C2X1W0QZK7M4N"
ART_ID = "art_01JA6Z9EAQ4C3N7V2B8M5X1K0T"
IMAGE = b"\x00\x01rps firmware image\xff" * 64
IMAGE_SHA = hashlib.sha256(IMAGE).hexdigest()


class FakeSiliconFlow:
    def __init__(self) -> None:
        self.build = {
            "id": BUILD_ID,
            "number": 42,
            "status": "passed",
            "profile": "siwx917-soc",
            "ref": "refs/heads/main",
            "commit_sha": COMMIT,
            "artifacts_state": "stored",
        }
        self.pending_polls = 0  # answer "running" this many times first
        self.listing = [
            {"id": "art_other", "file_name": "siwx917_soc.out", "sha256": "0" * 64, "size_bytes": 1},
            {"id": ART_ID, "file_name": "siwx917_soc.rps", "sha256": IMAGE_SHA, "size_bytes": len(IMAGE)},
        ]
        self.content = IMAGE
        self.header = IMAGE_SHA
        self.requests: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                url = urlparse(self.path)
                outer.requests.append(self.path)
                if url.path == "/api/v1/builds":
                    q = parse_qs(url.query)
                    match = q.get("gh_run_id") == ["777"] and q.get("gh_run_attempt") == ["2"]
                    return self._json(200, {"items": [{"id": BUILD_ID}] if match else [], "next_cursor": None})
                if url.path == f"/api/v1/builds/{BUILD_ID}":
                    if outer.pending_polls > 0:
                        outer.pending_polls -= 1
                        return self._json(200, {**outer.build, "status": "running", "artifacts_state": "pending"})
                    return self._json(200, outer.build)
                if url.path == f"/api/v1/builds/{BUILD_ID}/artifacts":
                    return self._json(200, outer.listing)
                if url.path == f"/api/v1/artifacts/{ART_ID}/download":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(outer.content)))
                    if outer.header is not None:
                        self.send_header("X-Checksum-SHA256", outer.header)
                    self.end_headers()
                    self.wfile.write(outer.content)
                    return None
                return self._json(404, {"detail": "not found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def api():
    with FakeSiliconFlow() as s:
        yield s


def fetch(tmp_path, api, *args):
    env = dict(os.environ, SFCI_API_URL=api.url, SFCI_OUT_DIR=str(tmp_path / "out"))
    if not any(a in args for a in ("--build-id", "--run-id")):
        args = ("--run-id", "777", "--run-attempt", "2", *args)
    return subprocess.run(
        [sys.executable, str(FETCH), "--dest", str(tmp_path / "dest"), "--wait", "2", "--interval", "0.05", *args],
        env=env, capture_output=True, text=True,
    )


def build_record(tmp_path):
    return json.loads((tmp_path / "out" / "hil" / "build.json").read_text())


def test_fetch_resolves_the_run_and_verifies_the_image(tmp_path, api):
    api.pending_polls = 2  # the backend has not settled the build yet
    r = fetch(tmp_path, api)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "dest" / "siwx917_soc.rps").read_bytes() == IMAGE
    assert sorted(p.name for p in (tmp_path / "dest").iterdir()) == ["siwx917_soc.rps"]
    record = build_record(tmp_path)
    assert record["build_id"] == BUILD_ID and record["commit_sha"] == COMMIT
    assert record["image"]["sha256"] == IMAGE_SHA and record["image"]["artifact_id"] == ART_ID
    assert f"sha256 {IMAGE_SHA} verified" in r.stdout


def test_fetch_by_build_id(tmp_path, api):
    r = fetch(tmp_path, api, "--build-id", BUILD_ID)
    assert r.returncode == 0, r.stderr
    assert not any(req.startswith("/api/v1/builds?") for req in api.requests)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda api: setattr(api, "content", IMAGE[:-1] + b"\x00"),  # same size, other bytes
        lambda api: setattr(api, "content", IMAGE + b"\x00"),  # longer
        lambda api: setattr(api, "header", "f" * 64),
        lambda api: setattr(api, "header", None),
    ],
)
def test_sha256_mismatch_leaves_no_image(tmp_path, api, tamper):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "siwx917_soc.rps").write_bytes(b"an older image")  # from an earlier run
    tamper(api)
    r = fetch(tmp_path, api)
    assert r.returncode == 1
    assert "SHA-256 mismatch" in r.stderr
    assert list(dest.iterdir()) == []
    # The build is known, so the run report can still name it.
    assert build_record(tmp_path)["build_id"] == BUILD_ID
    assert "image" not in build_record(tmp_path)


@pytest.mark.parametrize(
    "change, message",
    [
        ({"status": "failed"}, "status is failed"),
        ({"profile": "host-cmake"}, "profile is host-cmake"),
        ({"ref": "refs/heads/feature"}, "ref refs/heads/feature"),
        ({"ref": "refs/pull/7/merge"}, "ref refs/pull/7/merge"),
        ({"artifacts_state": "rejected"}, "artifacts are rejected"),
    ],
)
def test_unusable_builds_are_not_fetched(tmp_path, api, change, message):
    api.build.update(change)
    r = fetch(tmp_path, api)
    assert r.returncode == 1
    assert message in r.stderr
    assert not any("/download" in req for req in api.requests)


def test_tags_are_release_refs(tmp_path, api):
    api.build["ref"] = "refs/tags/v1.4.0"
    assert fetch(tmp_path, api).returncode == 0


def test_missing_artifact_fails(tmp_path, api):
    api.listing = api.listing[:1]
    r = fetch(tmp_path, api)
    assert r.returncode == 1
    assert "no single artifact siwx917_soc.rps (artifacts: siwx917_soc.out)" in r.stderr


def test_unknown_run_gives_up(tmp_path, api):
    r = fetch(tmp_path, api, "--run-id", "778")
    assert r.returncode == 1
    assert "gave up" in r.stderr
    assert not (tmp_path / "out" / "hil" / "build.json").exists()


def test_unreachable_backend_gives_up(tmp_path):
    env = dict(os.environ, SFCI_API_URL="http://127.0.0.1:9", SFCI_OUT_DIR=str(tmp_path / "out"))
    r = subprocess.run(
        [sys.executable, str(FETCH), "--build-id", BUILD_ID, "--dest", str(tmp_path / "d"), "--wait", "0.2", "--interval", "0.05"],
        env=env, capture_output=True, text=True,
    )
    assert r.returncode == 1 and "gave up" in r.stderr


@pytest.mark.parametrize(
    "args", [["--build-id", "../etc"], ["--build-id", BUILD_ID, "--file", "../x.rps"], ["--dest", "x"]]
)
def test_fetch_usage_errors(tmp_path, args):
    env = dict(os.environ, SFCI_API_URL="http://127.0.0.1:9")
    if "--dest" not in args:
        args = [*args, "--dest", str(tmp_path / "d")]
    r = subprocess.run([sys.executable, str(FETCH), *args], env=env, capture_output=True, text=True)
    assert r.returncode == 2
