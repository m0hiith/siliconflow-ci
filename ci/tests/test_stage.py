import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

STAGE = Path(__file__).resolve().parents[1] / "stage.sh"
ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def run_stage(tmp_path, *args, env_extra=None):
    env = dict(os.environ, SFCI_OUT_DIR=str(tmp_path / "out"))
    env.pop("GITHUB_ACTIONS", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(STAGE), *args], cwd=tmp_path, env=env, capture_output=True, text=True
    )


def read_lines(tmp_path):
    path = tmp_path / "out" / "stages.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_passing_stage_records_line_and_log(tmp_path):
    r = run_stage(tmp_path, "compile", "--", "sh", "-c", "echo hello; echo oops >&2")
    assert r.returncode == 0
    assert "hello" in r.stdout and "oops" in r.stdout  # shown live, merged

    log = (tmp_path / "out" / "logs" / "compile.log").read_text()
    assert "hello" in log and "oops" in log

    (line,) = read_lines(tmp_path)
    assert line["name"] == "compile"
    assert line["status"] == "passed"
    assert line["exit_code"] == 0
    assert set(line) == {"name", "status", "exit_code", "started_at", "finished_at"}


def test_failing_stage_propagates_exit_code(tmp_path):
    r = run_stage(tmp_path, "test", "--", "sh", "-c", "echo boom; exit 7")
    assert r.returncode == 7
    (line,) = read_lines(tmp_path)
    assert line["status"] == "failed"
    assert line["exit_code"] == 7
    assert "boom" in (tmp_path / "out" / "logs" / "test.log").read_text()


def test_missing_command_is_recorded_as_failure(tmp_path):
    r = run_stage(tmp_path, "configure", "--", "definitely-not-a-command-sfci")
    assert r.returncode == 127
    (line,) = read_lines(tmp_path)
    assert line["status"] == "failed" and line["exit_code"] == 127


def test_timestamps_are_iso8601_utc_and_ordered(tmp_path):
    run_stage(tmp_path, "a", "--", "sleep", "1")
    (line,) = read_lines(tmp_path)
    assert ISO_UTC.match(line["started_at"])
    assert ISO_UTC.match(line["finished_at"])
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    started = datetime.strptime(line["started_at"], fmt)
    finished = datetime.strptime(line["finished_at"], fmt)
    assert finished >= started


def test_lines_append_across_stages(tmp_path):
    run_stage(tmp_path, "configure", "--", "true")
    run_stage(tmp_path, "compile", "--", "false")
    assert [(l["name"], l["status"]) for l in read_lines(tmp_path)] == [
        ("configure", "passed"),
        ("compile", "failed"),
    ]


def test_arguments_are_passed_through_unchanged(tmp_path):
    r = run_stage(tmp_path, "args", "--", "printf", "%s|", "a b", "--", "$HOME")
    assert r.returncode == 0
    assert "a b|--|$HOME|" in r.stdout


def test_usage_errors_record_nothing(tmp_path):
    for args in ([], ["name"], ["name", "--"], ["name", "true"], ["name", "x", "true"]):
        r = run_stage(tmp_path, *args)
        assert r.returncode == 2, args
        assert "usage" in r.stderr
    assert not (tmp_path / "out" / "stages.jsonl").exists()


def test_invalid_stage_name_is_rejected(tmp_path):
    for bad in ['a"b', "../x", "a b", "a/b"]:
        r = run_stage(tmp_path, bad, "--", "true")
        assert r.returncode == 2, bad
    assert not (tmp_path / "out" / "stages.jsonl").exists()


def test_workflow_commands_only_inside_actions(tmp_path):
    local = run_stage(tmp_path, "x", "--", "false")
    assert "::" not in local.stdout

    ci = run_stage(tmp_path, "y", "--", "false", env_extra={"GITHUB_ACTIONS": "true"})
    assert "::group::stage y" in ci.stdout
    assert "::error title=stage y failed::" in ci.stdout
    # Workflow commands must not leak into the stage log.
    assert "::" not in (tmp_path / "out" / "logs" / "y.log").read_text()
