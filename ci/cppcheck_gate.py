#!/usr/bin/env python3
"""Summarise a cppcheck XML (v2) report and fail if it contains errors.

    python3 ci/cppcheck_gate.py <cppcheck.xml> <summary.json>

Prints each finding as `file:line:col: severity: message [id]`, writes counts
per severity to <summary.json>, and exits 1 if any finding has severity "error".
"""
import json
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

SEVERITIES = ("error", "warning", "style", "performance", "portability", "information")


def parse(xml_path: Path) -> list:
    root = ET.parse(xml_path).getroot()
    findings = []
    for err in root.iter("error"):
        loc = err.find("location")
        findings.append(
            {
                "id": err.get("id", ""),
                "severity": err.get("severity", ""),
                "message": err.get("msg", ""),
                "file": loc.get("file", "") if loc is not None else "",
                "line": int(loc.get("line", 0)) if loc is not None else 0,
                "column": int(loc.get("column", 0)) if loc is not None else 0,
            }
        )
    return findings


def summarise(findings: list) -> dict:
    counts = Counter(f["severity"] for f in findings)
    return {sev: counts.get(sev, 0) for sev in SEVERITIES}


def main(argv: list) -> int:
    if len(argv) != 3:
        print("usage: cppcheck_gate.py <cppcheck.xml> <summary.json>", file=sys.stderr)
        return 2
    findings = parse(Path(argv[1]))
    for f in findings:
        where = f"{f['file']}:{f['line']}:{f['column']}" if f["file"] else "<no location>"
        print(f"{where}: {f['severity']}: {f['message']} [{f['id']}]")

    summary = summarise(findings)
    Path(argv[2]).write_text(json.dumps(summary, indent=2) + "\n")
    print("cppcheck: " + ", ".join(f"{k}={v}" for k, v in summary.items()))

    if summary["error"] > 0:
        print(f"cppcheck: {summary['error']} error(s); failing", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
