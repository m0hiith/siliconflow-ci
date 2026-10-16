import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

CI_DIR = Path(__file__).resolve().parents[1]
REPORT = CI_DIR / "report.py"
FIXTURES = Path(__file__).parent / "fixtures" / "report"
REQUIRED = ["configure", "compile", "test", "analyze", "package"]

sys.path.insert(0, str(CI_DIR))
import report  # noqa: E402

ENV = {
    "GITHUB_REPOSITORY": "acme/fw",
    "GITHUB_RUN_ID": "30433642",
    "GITHUB_RUN_ATTEMPT": "2",
    "GITHUB_SHA": "3484a3fb816e0859fd6e1cea078d76385ff50625",
    "GITHUB_REF": "refs/heads/main",
    "GITHUB_JOB": "host-cmake",
    "GITHUB_WORKFLOW_REF": "acme/fw/.github/workflows/firmware-ci.yml@refs/heads/main",
}
TOOLCHAIN = {"gcc": "gcc (Ubuntu 13.3.0-6ubuntu2~24.04) 13.3.0", "cmake": "cmake version 3.28.3"}
NOW = datetime(2026, 10, 14, 9, 2, tzinfo=UTC)


def build(fixture: Path, required=REQUIRED, artifact_name="sfci-out-host-cmake-attempt-2"):
    return report.build_report(fixture, required, artifact_name, ENV, TOOLCHAIN, now=NOW)


def stage_summary(rep):
    return [(s["name"], s["status"], s["required"]) for s in rep["stages"]]


# ------------------------------------------------------------- fixtures


def test_complete_run(tmp_path):
    rep = build(FIXTURES / "complete")
    assert rep["schema"] == "sfci.run-report/1"
    assert rep["generated_at"] == "2026-10-14T09:02:00Z"
    assert rep["github"] == {
        "repository": "acme/fw",
        "run_id": 30433642,
        "run_attempt": 2,
        "sha": "3484a3fb816e0859fd6e1cea078d76385ff50625",
        "ref": "refs/heads/main",
        "job": "host-cmake",
        "workflow_ref": "acme/fw/.github/workflows/firmware-ci.yml@refs/heads/main",
    }
    assert stage_summary(rep) == [(name, "passed", True) for name in REQUIRED]
    compile_stage = rep["stages"][1]
    assert compile_stage == {
        "name": "compile",
        "status": "passed",
        "exit_code": 0,
        "started_at": "2026-10-14T09:01:04Z",
        "finished_at": "2026-10-14T09:01:20Z",
        "required": True,
    }
    assert rep["toolchain"] == TOOLCHAIN
    assert rep["sdk"] is None
    assert rep["coverage"] == {"line_percent": 91.25, "lines_covered": 73, "lines_valid": 80}
    assert rep["cppcheck"] == {
        "error": 0, "warning": 1, "style": 3, "performance": 0, "portability": 0, "information": 2
    }

    junit = FIXTURES / "complete" / "junit" / "ctest.xml"
    assert rep["junit"] == [
        {
            "path": "junit/ctest.xml",
            "size": junit.stat().st_size,
            "sha256": hashlib.sha256(junit.read_bytes()).hexdigest(),
        }
    ]

    assert rep["artifact_name"] == "sfci-out-host-cmake-attempt-2"
    manifest = rep["manifest"]
    assert manifest["schema"] == "sfci.artifact-manifest/1"
    for entry in manifest["files"]:
        data = (FIXTURES / "complete" / "artifacts" / entry["file"]).read_bytes()
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
    json.dumps(rep)  # serialisable as-is


def test_failed_compile_marks_later_required_stages_skipped():
    rep = build(FIXTURES / "failed-compile")
    assert stage_summary(rep) == [
        ("configure", "passed", True),
        ("compile", "failed", True),
        ("test", "skipped", True),
        ("analyze", "skipped", True),
        ("package", "skipped", True),
    ]
    skipped = rep["stages"][2]
    assert (skipped["exit_code"], skipped["started_at"], skipped["finished_at"]) == (None, None, None)
    assert rep["stages"][1]["exit_code"] == 1
    # Nothing else was produced, and the report says so instead of guessing.
    assert (rep["coverage"], rep["cppcheck"], rep["size"], rep["junit"], rep["manifest"]) == (
        None, None, None, [], None
    )


def test_empty_out_dir_reports_every_required_stage_skipped(tmp_path):
    rep = build(tmp_path)
    assert stage_summary(rep) == [(name, "skipped", True) for name in REQUIRED]


# --------------------------------------------------------------- stages


def test_optional_stages_are_kept_and_not_required(tmp_path):
    (tmp_path / "stages.jsonl").write_text(
        '{"name":"lint","status":"failed","exit_code":3}\n'
        '{"name":"compile","status":"passed","exit_code":0}\n'
    )
    rep = build(tmp_path, required=["compile", "test"])
    assert stage_summary(rep) == [
        ("lint", "failed", False),
        ("compile", "passed", True),
        ("test", "skipped", True),
    ]


def test_malformed_and_unknown_lines_are_ignored(tmp_path, capsys):
    (tmp_path / "stages.jsonl").write_text(
        "not json\n"
        '{"name":"compile"}\n'
        '{"name":"test","status":"exploded"}\n'
        "\n"
        '{"name":"configure","status":"passed","exit_code":0}\n'
    )
    rep = build(tmp_path, required=["configure", "compile", "test"])
    assert stage_summary(rep) == [
        ("configure", "passed", True),
        ("compile", "skipped", True),
        ("test", "skipped", True),
    ]
    err = capsys.readouterr().err
    assert "stages.jsonl:1: malformed" in err
    assert "stages.jsonl:2: malformed" in err
    assert "unknown stage status 'exploded'" in err


def test_a_stage_run_twice_is_reported_by_its_last_run(tmp_path):
    (tmp_path / "stages.jsonl").write_text(
        '{"name":"test","status":"failed","exit_code":8}\n'
        '{"name":"compile","status":"passed","exit_code":0}\n'
        '{"name":"test","status":"passed","exit_code":0}\n'
    )
    rep = build(tmp_path, required=[])
    assert stage_summary(rep) == [("compile", "passed", False), ("test", "passed", False)]


# ------------------------------------------------------- optional inputs


def test_coverage_falls_back_to_line_rate(tmp_path):
    (tmp_path / "coverage").mkdir()
    (tmp_path / "coverage" / "coverage.xml").write_text('<coverage line-rate="0.5"/>')
    assert build(tmp_path)["coverage"] == {
        "line_percent": 50.0, "lines_covered": None, "lines_valid": None
    }


@pytest.mark.parametrize("content", ["<coverage", "<coverage/>", '<coverage line-rate="x"/>'])
def test_unusable_coverage_is_null(tmp_path, content):
    (tmp_path / "coverage").mkdir()
    (tmp_path / "coverage" / "coverage.xml").write_text(content)
    assert build(tmp_path)["coverage"] is None


@pytest.mark.parametrize("content", ["{", '{"error": "many"}', "[1, 2]"])
def test_unusable_cppcheck_summary_is_null(tmp_path, content):
    (tmp_path / "analysis").mkdir()
    (tmp_path / "analysis" / "cppcheck-summary.json").write_text(content)
    assert build(tmp_path)["cppcheck"] is None


SIZE_JSON = {
    "file": "firmware.elf",
    "text_bytes": 8448,
    "data_bytes": 100,
    "bss_bytes": 8564,
    "sections": [{"name": ".isr_vector", "size": 64, "addr": 0}, {"name": ".text", "size": 8376, "addr": 64}],
}


def test_size_is_reported_from_size_json(tmp_path):
    (tmp_path / "size").mkdir()
    (tmp_path / "size" / "size.json").write_text(json.dumps(SIZE_JSON))
    assert build(tmp_path)["size"] == SIZE_JSON


def test_size_drops_malformed_sections(tmp_path):
    (tmp_path / "size").mkdir()
    sections = [*SIZE_JSON["sections"], {"name": ".x", "size": -1, "addr": 0}, "junk"]
    (tmp_path / "size" / "size.json").write_text(json.dumps({**SIZE_JSON, "sections": sections}))
    assert build(tmp_path)["size"]["sections"] == SIZE_JSON["sections"]


@pytest.mark.parametrize(
    "content",
    ["{", "[]", json.dumps({**SIZE_JSON, "text_bytes": -1}), json.dumps({**SIZE_JSON, "file": None}),
     json.dumps({k: v for k, v in SIZE_JSON.items() if k != "bss_bytes"})],
)
def test_unusable_size_is_null(tmp_path, content):
    (tmp_path / "size").mkdir()
    (tmp_path / "size" / "size.json").write_text(content)
    assert build(tmp_path)["size"] is None


def test_manifest_with_another_schema_is_ignored(tmp_path):
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "manifest.json").write_text('{"schema": "other/1", "files": []}')
    assert build(tmp_path)["manifest"] is None


def test_artifact_name_is_null_when_not_given(tmp_path):
    assert build(tmp_path, artifact_name=None)["artifact_name"] is None
    assert build(tmp_path, artifact_name="")["artifact_name"] is None


def test_junit_files_are_found_recursively_and_sorted(tmp_path):
    (tmp_path / "junit" / "unit").mkdir(parents=True)
    (tmp_path / "junit" / "unit" / "b.xml").write_text("<testsuite/>")
    (tmp_path / "junit" / "a.xml").write_text("<testsuites/>")
    (tmp_path / "junit" / "notes.txt").write_text("not junit")
    assert [j["path"] for j in build(tmp_path)["junit"]] == ["junit/a.xml", "junit/unit/b.xml"]


def test_github_context_outside_actions_is_null():
    assert report.github_context({}) == {
        "repository": None, "run_id": None, "run_attempt": None, "sha": None,
        "ref": None, "job": None, "workflow_ref": None,
    }


# ------------------------------------------------------------------ CLI


def fake_tool(bin_dir: Path, name: str, output: str, code: int = 0) -> None:
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\nexit {code}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def run_cli(tmp_path, out_dir, *args, bin_dir):
    env = {**os.environ, **ENV, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(
        [sys.executable, str(REPORT), "--out", str(out_dir), *args],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )


def test_cli_writes_report_with_toolchain_versions(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_tool(bin_dir, "gcc", "gcc (SFCI) 13.3.0")
    fake_tool(bin_dir, "arm-none-eabi-gcc", "arm-none-eabi-gcc (SFCI) 13.2.1")
    fake_tool(bin_dir, "cmake", "cmake version 3.28.3")
    out = tmp_path / "out"
    shutil.copytree(FIXTURES / "complete", out)

    r = run_cli(
        tmp_path, out, "--required", "configure,compile,test,analyze,package",
        "--artifact-name", "sfci-out-host-cmake-attempt-2", bin_dir=bin_dir,
    )
    assert r.returncode == 0, r.stderr
    assert "stage compile: passed (required)" in r.stdout

    rep = json.loads((out / "report.json").read_text())
    assert rep["toolchain"] == {
        "gcc": "gcc (SFCI) 13.3.0",
        "arm-none-eabi-gcc": "arm-none-eabi-gcc (SFCI) 13.2.1",
        "cmake": "cmake version 3.28.3",
    }
    assert rep["github"]["run_id"] == 30433642
    assert rep["artifact_name"] == "sfci-out-host-cmake-attempt-2"
    assert len(rep["manifest"]["files"]) == 2
    # The report is not one of the JUnit files it lists.
    assert [j["path"] for j in rep["junit"]] == ["junit/ctest.xml"]


def test_cli_missing_or_failing_tools_are_null(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_tool(bin_dir, "gcc", "boom", code=1)
    # No cmake on this PATH at all.
    env_path = str(bin_dir)
    r = subprocess.run(
        [sys.executable, str(REPORT), "--out", str(tmp_path / "out"),
         "--output", str(tmp_path / "r.json")],
        cwd=tmp_path, env={**ENV, "PATH": env_path}, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    rep = json.loads((tmp_path / "r.json").read_text())
    assert rep["toolchain"] == {"gcc": None, "arm-none-eabi-gcc": None, "cmake": None}
    assert rep["stages"] == []  # no stages ran and none were required


VERSIONS = {
    "sdk": {"simplicity_sdk": "2026.6.1", "wiseconnect": "4.1.1"},
    "toolchain": {"slc_cli": "6.0.23", "arm-none-eabi-gcc": "14.2.1"},
}


def test_cli_versions_file_supplies_sdk_and_toolchain(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Tools on PATH are not probed when the image declares its versions.
    fake_tool(bin_dir, "gcc", "gcc (host) 13.3.0")
    versions = tmp_path / "versions.json"
    versions.write_text(json.dumps(VERSIONS))
    out = tmp_path / "out"

    r = run_cli(tmp_path, out, "--versions", str(versions), bin_dir=bin_dir)
    assert r.returncode == 0, r.stderr
    rep = json.loads((out / "report.json").read_text())
    assert rep["sdk"] == VERSIONS["sdk"]
    assert rep["toolchain"] == VERSIONS["toolchain"]


@pytest.mark.parametrize(
    "content",
    [
        None,  # file missing
        "not json",
        "[]",
        json.dumps({"sdk": "2026.6.1", "toolchain": {"gcc": 14}}),
        json.dumps({"sdk": {}, "toolchain": []}),
    ],
)
def test_cli_unusable_versions_file_is_null(tmp_path, content):
    versions = tmp_path / "versions.json"
    if content is not None:
        versions.write_text(content)
    out = tmp_path / "out"

    r = run_cli(tmp_path, out, "--versions", str(versions), bin_dir=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "versions" in r.stderr
    rep = json.loads((out / "report.json").read_text())
    assert rep["sdk"] is None
    assert rep["toolchain"] == {}


def test_cli_versions_file_keeps_a_usable_section(tmp_path):
    versions = tmp_path / "versions.json"
    versions.write_text(json.dumps({"sdk": VERSIONS["sdk"], "toolchain": {"gcc": None}}))
    out = tmp_path / "out"

    r = run_cli(tmp_path, out, "--versions", str(versions), bin_dir=tmp_path)
    assert r.returncode == 0, r.stderr
    rep = json.loads((out / "report.json").read_text())
    assert rep["sdk"] == VERSIONS["sdk"]
    assert rep["toolchain"] == {}


# --------------------------------------------------------- linked reports


def test_linked_report_names_its_profile_and_build(tmp_path):
    rep = report.build_report(
        FIXTURES / "complete", ["fetch", "flash", "test"], None, ENV, TOOLCHAIN, now=NOW,
        link=("siwx917-hil", "bld_01JA6Z9D5V8R3C2X1W0QZK7M4N"),
    )
    assert rep["profile"] == "siwx917-hil"
    assert rep["build_id"] == "bld_01JA6Z9D5V8R3C2X1W0QZK7M4N"


def test_build_reports_have_no_link_fields():
    rep = build(FIXTURES / "complete")
    assert "profile" not in rep and "build_id" not in rep


def test_cli_link_flags(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "stages.jsonl").write_text(
        '{"name":"fetch","status":"passed","exit_code":0,"started_at":"2026-10-15T10:00:00Z",'
        '"finished_at":"2026-10-15T10:00:05Z"}\n'
    )
    args = [sys.executable, str(REPORT), "--out", str(out), "--required", "fetch,flash,test", "--versions", str(tmp_path / "none.json")]
    r = subprocess.run([*args, "--profile", "siwx917-hil", "--build-id", "bld_01J"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    rep = json.loads((out / "report.json").read_text())
    assert (rep["profile"], rep["build_id"]) == ("siwx917-hil", "bld_01J")
    assert [(s["name"], s["status"]) for s in rep["stages"]] == [
        ("fetch", "passed"), ("flash", "skipped"), ("test", "skipped"),
    ]

    r = subprocess.run([*args, "--profile", "siwx917-hil"], capture_output=True, text=True)
    assert r.returncode == 2 and "go together" in r.stderr
