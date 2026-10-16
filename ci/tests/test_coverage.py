import os
import stat
import subprocess
from pathlib import Path

COVERAGE = Path(__file__).resolve().parents[1] / "coverage.sh"


def fake_cmake(tmp_path, *, fail_on: str | None = None):
    """A cmake stand-in that logs its arguments; `--target coverage` writes coverage.xml."""
    path = tmp_path / "bin" / "cmake"
    path.parent.mkdir()
    path.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{tmp_path}/cmake.log"\n'
        f'case "$*" in *"{fail_on or "@never@"}"*) exit 1;; esac\n'
        'case "$*" in *"--target coverage"*) printf "<coverage/>" > "$2/coverage.xml";; esac\n'
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path.parent


def run(tmp_path, *args, bin_dir):
    env = dict(os.environ, SFCI_OUT_DIR=str(tmp_path / "out"), PATH=f"{bin_dir}:{os.environ['PATH']}")
    (tmp_path / "build-cov").mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", str(COVERAGE), *args], cwd=tmp_path, env=env, capture_output=True, text=True
    )


def test_instrumented_build_then_report_is_copied(tmp_path):
    r = run(tmp_path, "build-cov", bin_dir=fake_cmake(tmp_path))
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "cmake.log").read_text().splitlines() == [
        "-S firmware -B build-cov -G Ninja -DSFCI_COVERAGE=ON",
        "--build build-cov",
        "--build build-cov --target coverage",
    ]
    assert (tmp_path / "out" / "coverage" / "coverage.xml").read_text() == "<coverage/>"


def test_failing_tests_fail_the_stage_and_write_nothing(tmp_path):
    r = run(tmp_path, "build-cov", bin_dir=fake_cmake(tmp_path, fail_on="--target coverage"))
    assert r.returncode != 0
    assert not (tmp_path / "out" / "coverage").exists()


def test_usage(tmp_path):
    r = run(tmp_path, bin_dir=fake_cmake(tmp_path))
    assert r.returncode == 2 and "usage" in r.stderr
