import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

CI_DIR = Path(__file__).resolve().parents[1]
SIZE = CI_DIR / "size.py"
# Real arm-none-eabi-size 2.42 output for the cortex-m4 profile's firmware.elf.
FIXTURES = Path(__file__).parent / "fixtures" / "size"
SYSV = (FIXTURES / "cortex-m4.sysv.txt").read_text()
BERKELEY = (FIXTURES / "cortex-m4.berkeley.txt").read_text()

sys.path.insert(0, str(CI_DIR))
import size  # noqa: E402


def test_parse_sysv_lists_every_section():
    sections = size.parse_sysv(SYSV)
    assert sections[:5] == [
        {"name": ".isr_vector", "size": 64, "addr": 0},
        {"name": ".text", "size": 8376, "addr": 64},
        {"name": ".ARM.exidx", "size": 8, "addr": 8440},
        {"name": ".init_array", "size": 8, "addr": 8448},
        {"name": ".data", "size": 92, "addr": 0x20000000},
    ]
    assert len(sections) == 18
    assert sum(s["size"] for s in sections) == 144113  # the "Total" line


def test_parse_berkeley_totals():
    # text = .isr_vector + .text + .ARM.exidx; the writable .init_array counts as data.
    assert size.parse_berkeley(BERKELEY) == {"text_bytes": 8448, "data_bytes": 100, "bss_bytes": 8564}


@pytest.mark.parametrize("output", ["", "text data bss\n", "a b c\n1 2 3\n", BERKELEY + BERKELEY])
def test_unexpected_berkeley_output_is_an_error(output):
    with pytest.raises(size.SizeError):
        size.parse_berkeley(output)


def fake_size(tmp_path: Path, code: int = 0) -> Path:
    """A size tool stand-in that prints the fixture for -A or -B."""
    (tmp_path / "sysv.txt").write_text(SYSV)
    (tmp_path / "berkeley.txt").write_text(BERKELEY)
    path = tmp_path / "fake-size"
    path.write_text(
        "#!/bin/sh\n"
        f'[ {code} -ne 0 ] && {{ echo "fake-size: boom" >&2; exit {code}; }}\n'
        f'case "$1" in -A) cat "{tmp_path}/sysv.txt" ;; -B) cat "{tmp_path}/berkeley.txt" ;; esac\n'
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def run_size(tmp_path: Path, *args: str, env: dict | None = None):
    return subprocess.run(
        [sys.executable, str(SIZE), *args],
        cwd=tmp_path, env={**os.environ, **(env or {})}, capture_output=True, text=True,
    )


def test_cli_writes_size_json(tmp_path):
    (tmp_path / "firmware.elf").write_bytes(b"\x7fELF")
    tool = fake_size(tmp_path)
    r = run_size(tmp_path, "--size-tool", str(tool), "--out", "out", "firmware.elf")
    assert r.returncode == 0, r.stderr
    assert "firmware.elf: text 8448  data 100  bss 8564 bytes" in r.stdout

    result = json.loads((tmp_path / "out" / "size" / "size.json").read_text())
    assert {k: result[k] for k in ("file", "text_bytes", "data_bytes", "bss_bytes")} == {
        "file": "firmware.elf", "text_bytes": 8448, "data_bytes": 100, "bss_bytes": 8564,
    }
    assert result["sections"][1] == {"name": ".text", "size": 8376, "addr": 64}


def test_cli_takes_the_tool_and_out_dir_from_the_environment(tmp_path):
    (tmp_path / "firmware.elf").write_bytes(b"\x7fELF")
    env = {"SIZE": str(fake_size(tmp_path)), "SFCI_OUT_DIR": "elsewhere"}
    assert run_size(tmp_path, "firmware.elf", env=env).returncode == 0
    assert (tmp_path / "elsewhere" / "size" / "size.json").is_file()


def test_cli_fails_when_the_tool_fails_or_the_file_is_missing(tmp_path):
    (tmp_path / "firmware.elf").write_bytes(b"\x7fELF")
    r = run_size(tmp_path, "--size-tool", str(fake_size(tmp_path, code=3)), "firmware.elf")
    assert r.returncode == 1 and "boom" in r.stderr
    assert not (tmp_path / "out").exists()

    r = run_size(tmp_path, "--size-tool", str(tmp_path / "no-such-tool"), "firmware.elf")
    assert r.returncode == 1

    r = run_size(tmp_path, "--size-tool", str(fake_size(tmp_path)), "missing.elf")
    assert r.returncode == 1 and "no such file" in r.stderr
