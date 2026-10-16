import json
import subprocess
import sys
from pathlib import Path

GATE = Path(__file__).resolve().parents[1] / "cppcheck_gate.py"

HEADER = '<?xml version="1.0" encoding="UTF-8"?>\n<results version="2"><cppcheck version="2.13.0"/><errors>'
FOOTER = "</errors></results>\n"


def finding(severity, ident, msg, file="firmware/src/a.c", line=10, column=5):
    return (
        f'<error id="{ident}" severity="{severity}" msg="{msg}" verbose="{msg}">'
        f'<location file="{file}" line="{line}" column="{column}"/></error>'
    )


def run_gate(tmp_path, *findings):
    xml = tmp_path / "cppcheck.xml"
    xml.write_text(HEADER + "".join(findings) + FOOTER)
    summary = tmp_path / "summary.json"
    r = subprocess.run(
        [sys.executable, str(GATE), str(xml), str(summary)], capture_output=True, text=True
    )
    return r, json.loads(summary.read_text())


def test_clean_report_passes(tmp_path):
    r, summary = run_gate(tmp_path)
    assert r.returncode == 0
    assert all(v == 0 for v in summary.values())


def test_warnings_and_style_do_not_fail(tmp_path):
    r, summary = run_gate(
        tmp_path,
        finding("warning", "uninitvar", "maybe"),
        finding("style", "constVariable", "could be const"),
    )
    assert r.returncode == 0
    assert summary["warning"] == 1 and summary["style"] == 1 and summary["error"] == 0


def test_error_fails_and_is_printed_with_location(tmp_path):
    r, summary = run_gate(
        tmp_path,
        finding("error", "arrayIndexOutOfBounds", "Array index 8 is out of bounds.", line=42),
        finding("warning", "x", "y"),
    )
    assert r.returncode == 1
    assert summary["error"] == 1
    assert (
        "firmware/src/a.c:42:5: error: Array index 8 is out of bounds. [arrayIndexOutOfBounds]"
        in r.stdout
    )


def test_finding_without_location(tmp_path):
    r, summary = run_gate(
        tmp_path,
        '<error id="checkersReport" severity="information" msg="Active checkers"/>',
    )
    assert r.returncode == 0
    assert summary["information"] == 1
    assert "<no location>" in r.stdout
