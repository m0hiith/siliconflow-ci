#!/usr/bin/env python3
"""Collect the facts of one CI job into a run report (schema sfci.run-report/1).

    python3 ci/report.py --out out --required configure,compile,test \\
        --artifact-name sfci-out-host-cmake-attempt-1 [--output out/report.json]

Reads, from the out directory:
  stages.jsonl                    one line per stage run by ci/stage.sh
  junit/**/*.xml                  JUnit files (listed with size and SHA-256)
  coverage/coverage.xml           Cobertura coverage, optional
  analysis/cppcheck-summary.json  cppcheck counts per severity, optional
  size/size.json                  ELF text/data/bss and sections (ci/size.py), optional
  artifacts/manifest.json         the artifact manifest, optional

With --versions, the "sdk" and "toolchain" fields come from that file, the
versions a toolchain image declares (e.g. /opt/sfci/versions.json):

    {"sdk": {"simplicity_sdk": "2026.6.1", "wiseconnect": "4.1.1"},
     "toolchain": {"slc_cli": "6.0.23", "arm-none-eabi-gcc": "14.2.1"}}

Without it, "sdk" is null and "toolchain" comes from `<tool> --version`.

With --profile and --build-id the report is a linked report: it adds to the
build it names instead of describing a build of its own run. The
hardware-in-the-loop workflow (hil.yml) posts one with profile siwx917-hil;
see docs/hil.md.

A required stage with no line in stages.jsonl never ran and is reported as
"skipped". Missing optional inputs are reported as null. The report states
facts only: the backend decides the build's status.
"""
import argparse
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from manifest import sha256_file

SCHEMA = "sfci.run-report/1"
MANIFEST_SCHEMA = "sfci.artifact-manifest/1"
STAGE_STATUSES = ("passed", "failed")
TOOLCHAIN_COMMANDS = {
    "gcc": ["gcc", "--version"],
    "arm-none-eabi-gcc": ["arm-none-eabi-gcc", "--version"],
    "cmake": ["cmake", "--version"],
}
SIZE_TOTALS = ("text_bytes", "data_bytes", "bss_bytes")


def warn(message: str) -> None:
    print(f"report.py: {message}", file=sys.stderr)


def read_stages(out_dir: Path, required: list[str]) -> list[dict]:
    """Stages in the order they ran, then required stages that never ran."""
    by_name: dict[str, dict] = {}
    path = out_dir / "stages.jsonl"
    lines = path.read_text().splitlines() if path.is_file() else []
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            name, status = entry["name"], entry["status"]
        except (ValueError, KeyError, TypeError):
            warn(f"{path}:{n}: malformed stage line ignored")
            continue
        if status not in STAGE_STATUSES:
            warn(f"{path}:{n}: unknown stage status {status!r} ignored")
            continue
        # A stage run twice in one job is reported by its last run.
        by_name.pop(name, None)
        by_name[name] = {
            "name": name,
            "status": status,
            "exit_code": entry.get("exit_code"),
            "started_at": entry.get("started_at"),
            "finished_at": entry.get("finished_at"),
        }
    for name in required:
        if name not in by_name:
            by_name[name] = {
                "name": name,
                "status": "skipped",
                "exit_code": None,
                "started_at": None,
                "finished_at": None,
            }
    return [{**stage, "required": stage["name"] in required} for stage in by_name.values()]


def tool_version(cmd: list[str]) -> str | None:
    """First line of `<tool> --version`, or None if the tool is unavailable."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout.strip().splitlines()[0]


def read_toolchain() -> dict:
    return {name: tool_version(cmd) for name, cmd in TOOLCHAIN_COMMANDS.items()}


def read_versions(path: Path) -> tuple[dict | None, dict]:
    """(sdk, toolchain) from an image's versions file. A missing or malformed
    file, or section, is reported as null sdk / empty toolchain."""
    versions = read_json(path, "SDK and toolchain versions")
    if versions is None:
        warn(f"{path}: not found or unreadable; SDK and toolchain versions omitted")
        return None, {}
    if not isinstance(versions, dict):
        warn(f"{path}: not a JSON object; SDK and toolchain versions omitted")
        return None, {}

    def section(name: str) -> dict | None:
        value = versions.get(name)
        if isinstance(value, dict) and value and all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()
        ):
            return dict(value)
        warn(f"{path}: no usable {name!r} object of name -> version strings; omitted")
        return None

    return section("sdk"), section("toolchain") or {}


def read_coverage(out_dir: Path) -> dict | None:
    path = out_dir / "coverage" / "coverage.xml"
    if not path.is_file():
        return None
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        warn(f"{path}: not valid XML; coverage omitted")
        return None
    covered, valid = root.get("lines-covered"), root.get("lines-valid")
    try:
        if covered is not None and valid is not None:
            lines_covered, lines_valid = int(covered), int(valid)
            percent = 100.0 * lines_covered / lines_valid if lines_valid else 0.0
        else:
            lines_covered = lines_valid = None
            percent = 100.0 * float(root.get("line-rate", ""))
    except ValueError:
        warn(f"{path}: no usable line counts; coverage omitted")
        return None
    return {
        "line_percent": round(percent, 2),
        "lines_covered": lines_covered,
        "lines_valid": lines_valid,
    }


def read_json(path: Path, what: str) -> object | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except ValueError:
        warn(f"{path}: not valid JSON; {what} omitted")
        return None


def read_cppcheck(out_dir: Path) -> dict | None:
    summary = read_json(out_dir / "analysis" / "cppcheck-summary.json", "cppcheck counts")
    if not isinstance(summary, dict) or not all(isinstance(v, int) for v in summary.values()):
        return None
    return summary


def read_size(out_dir: Path) -> dict | None:
    size = read_json(out_dir / "size" / "size.json", "size")
    if not isinstance(size, dict):
        return None
    totals = [size.get(key) for key in SIZE_TOTALS]
    sections = size.get("sections", [])
    if (
        not isinstance(size.get("file"), str)
        or not all(isinstance(n, int) and n >= 0 for n in totals)
        or not isinstance(sections, list)
    ):
        warn(f"{out_dir / 'size' / 'size.json'}: unexpected content; size omitted")
        return None
    return {
        "file": size["file"],
        **dict(zip(SIZE_TOTALS, totals)),
        "sections": [
            {"name": s["name"], "size": s["size"], "addr": s["addr"]}
            for s in sections
            if isinstance(s, dict)
            and isinstance(s.get("name"), str)
            and all(isinstance(s.get(k), int) and s[k] >= 0 for k in ("size", "addr"))
        ],
    }


def list_junit(out_dir: Path) -> list[dict]:
    junit_dir = out_dir / "junit"
    if not junit_dir.is_dir():
        return []
    return [
        {
            "path": path.relative_to(out_dir).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(junit_dir.rglob("*.xml"))
        if path.is_file()
    ]


def read_manifest(out_dir: Path) -> dict | None:
    manifest = read_json(out_dir / "artifacts" / "manifest.json", "artifact manifest")
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        return None
    return manifest


def github_context(env: dict[str, str]) -> dict:
    def as_int(name: str) -> int | None:
        value = env.get(name)
        return int(value) if value and value.isdigit() else None

    return {
        "repository": env.get("GITHUB_REPOSITORY"),
        "run_id": as_int("GITHUB_RUN_ID"),
        "run_attempt": as_int("GITHUB_RUN_ATTEMPT"),
        "sha": env.get("GITHUB_SHA"),
        "ref": env.get("GITHUB_REF"),
        "job": env.get("GITHUB_JOB"),
        "workflow_ref": env.get("GITHUB_WORKFLOW_REF"),
    }


def build_report(
    out_dir: Path,
    required: list[str],
    artifact_name: str | None,
    env: dict[str, str],
    toolchain: dict,
    now: datetime | None = None,
    sdk: dict | None = None,
    link: tuple[str, str] | None = None,
) -> dict:
    """`link` is (profile, build_id) for a linked report."""
    now = now or datetime.now(UTC)
    report = {
        "schema": SCHEMA,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "github": github_context(env),
        "stages": read_stages(out_dir, required),
        "sdk": sdk,
        "toolchain": toolchain,
        "coverage": read_coverage(out_dir),
        "cppcheck": read_cppcheck(out_dir),
        "size": read_size(out_dir),
        "junit": list_junit(out_dir),
        # The Actions artifact that holds this out directory, zipped. The
        # backend downloads it to verify the manifest and read the JUnit files.
        "artifact_name": artifact_name or None,
        "manifest": read_manifest(out_dir),
    }
    if link is not None:
        report["profile"], report["build_id"] = link
    return report


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", default=os.environ.get("SFCI_OUT_DIR", "out"), type=Path)
    p.add_argument(
        "--required",
        default="",
        help="comma-separated stage names that must run (missing ones become 'skipped')",
    )
    p.add_argument("--artifact-name", help="name of the Actions artifact holding the out directory")
    p.add_argument("--output", type=Path, help="default: <out>/report.json")
    p.add_argument(
        "--versions",
        type=Path,
        help="JSON file of SDK and toolchain versions (replaces `<tool> --version` probing)",
    )
    p.add_argument("--profile", help="linked report: its profile, e.g. siwx917-hil (with --build-id)")
    p.add_argument("--build-id", help="linked report: the SiliconFlow build it adds to")
    args = p.parse_args(argv)
    if (args.profile is None) != (args.build_id is None):
        p.error("--profile and --build-id go together")
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    required = [name for name in args.required.split(",") if name]
    if args.versions is not None:
        sdk, toolchain = read_versions(args.versions)
    else:
        sdk, toolchain = None, read_toolchain()
    link = (args.profile, args.build_id) if args.profile is not None else None
    report = build_report(
        args.out, required, args.artifact_name, dict(os.environ), toolchain, sdk=sdk, link=link
    )
    output = args.output or args.out / "report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")

    for stage in report["stages"]:
        flag = " (required)" if stage["required"] else ""
        print(f"stage {stage['name']}: {stage['status']}{flag}")
    print(f"run report written to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
