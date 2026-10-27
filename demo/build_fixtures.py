#!/usr/bin/env python3
"""Build demo/fixtures/{reports,artifacts,github,gates} from their sources.

    python3 demo/build_fixtures.py           # (re)write the fixtures
    python3 demo/build_fixtures.py --check   # exit 1 if any is out of date

Captured fixtures come unchanged from backend/tests/fixtures (real CI output,
see the READMEs there). Synthetic ones are assembled here from captured logs
and tool output where they exist, and are listed in demo/README.md. Reports
for assembled runs are written by the runner's own ci/report.py, and ELF
sizes by ci/size.py's parser, so they have exactly the shape a runner posts.
Zips are written with fixed timestamps and order, so a rebuild is
byte-identical.
"""
import argparse
import io
import json
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

DEMO = Path(__file__).resolve().parent
REPO = DEMO.parent
FIXTURES = DEMO / "fixtures"
BACKEND_FIXTURES = REPO / "backend" / "tests" / "fixtures"
CI_RUNS = BACKEND_FIXTURES / "ci_runs"
LOGS = BACKEND_FIXTURES / "logs"

sys.path.insert(0, str(REPO / "ci"))
import report as ci_report  # noqa: E402  (ci/report.py)
import size as ci_size  # noqa: E402  (ci/size.py)

ZIP_TIME = (2026, 10, 15, 0, 0, 0)
HOST_STAGES = ["configure", "compile", "test", "analyze", "package"]
# The identity is rewritten at replay time (app.demo.scenarios.run_report).
PLACEHOLDER_ENV = {
    "GITHUB_REPOSITORY": "siliconflow-demo/firmware",
    "GITHUB_RUN_ID": "1",
    "GITHUB_RUN_ATTEMPT": "1",
    "GITHUB_SHA": "0" * 40,
    "GITHUB_REF": "refs/heads/main",
}


def _captured_toolchain() -> dict:
    return json.loads((CI_RUNS / "passed" / "report.json").read_text())["toolchain"]


def stage(name: str, status: str, exit_code: int | None, start: str, end: str) -> dict:
    return {"name": name, "status": status, "exit_code": exit_code, "started_at": start, "finished_at": end}


@dataclass
class Assembled:
    """A synthetic run: its out/ directory is built from these parts, and its
    report by ci/report.py."""

    job: str
    required: list[str]
    stages: list[dict]
    files: dict[str, Path] = field(default_factory=dict)  # out/ path -> source
    size: dict | None = None
    toolchain: dict = field(default_factory=dict)
    sdk: dict | None = None
    link: tuple[str, str] | None = None  # linked report: (profile, build id)
    artifact: bool = True

    def out_dir(self, root: Path) -> Path:
        out = root / "out"
        out.mkdir()
        (out / "stages.jsonl").write_text("".join(json.dumps(s) + "\n" for s in self.stages))
        for rel, src in self.files.items():
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, out / rel)
        if self.size is not None:
            (out / "size").mkdir()
            (out / "size" / "size.json").write_text(json.dumps(self.size))
        return out

    def report(self, out: Path) -> dict:
        env = {**PLACEHOLDER_ENV, "GITHUB_JOB": self.job}
        last = max(s["finished_at"] for s in self.stages if s.get("finished_at"))
        return ci_report.build_report(
            out,
            self.required,
            f"sfci-out-{self.job}-attempt-1" if self.artifact else None,
            env,
            self.toolchain,
            now=datetime.fromisoformat(last.replace("Z", "+00:00")).astimezone(UTC),
            sdk=self.sdk,
            link=self.link,
        )


def cortex_m4_size() -> dict:
    """GNU size output captured from the cortex-m4 build (ci/tests/fixtures/size)."""
    sizes = REPO / "ci" / "tests" / "fixtures" / "size"
    berkeley = ci_size.parse_berkeley((sizes / "cortex-m4.berkeley.txt").read_text())
    sections = ci_size.parse_sysv((sizes / "cortex-m4.sysv.txt").read_text())
    return {"file": "firmware.elf", **berkeley, "sections": sections}


T = "2026-10-15T07:00:"
ASSEMBLED: dict[str, Assembled] = {
    # The README's demo step 4: an undeclared identifier in sensor_logic.c.
    "host-cmake-compile-error": Assembled(
        job="host-cmake",
        required=HOST_STAGES,
        stages=[
            stage("configure", "passed", 0, T + "00Z", T + "04Z"),
            stage("compile", "failed", 1, T + "04Z", T + "07Z"),
        ],
        files={
            "logs/configure.log": LOGS / "clean.configure.log",
            "logs/compile.log": LOGS / "gcc-undeclared.compile.log",
        },
        toolchain=_captured_toolchain(),
    ),
    "cortex-m4-passed": Assembled(
        job="cortex-m4",
        required=["configure", "compile", "package", "size"],
        stages=[
            stage("configure", "passed", 0, T + "00Z", T + "03Z"),
            stage("compile", "passed", 0, T + "03Z", T + "21Z"),
            stage("package", "passed", 0, T + "21Z", T + "22Z"),
            stage("size", "passed", 0, T + "22Z", T + "23Z"),
        ],
        size=cortex_m4_size(),
    ),
    "siwx917-soc-passed": Assembled(
        job="siwx917-soc",
        required=["generate", "compile", "package"],
        stages=[
            stage("generate", "passed", 0, T + "00Z", T + "15Z"),
            stage("compile", "passed", 0, T + "15Z", T + "48Z"),
            stage("package", "passed", 0, T + "48Z", T + "50Z"),
        ],
    ),
    # hil.yml's report when the runner has no J-Link: ci/hil/flash.sh exits 75.
    "hil-no-jlink": Assembled(
        job="hil",
        required=["fetch", "flash", "test"],
        stages=[
            stage("fetch", "passed", 0, T + "00Z", T + "06Z"),
            stage("flash", "failed", 75, T + "06Z", T + "09Z"),
        ],
        link=("siwx917-hil", "bld_00000000000000000000000000"),
        artifact=False,
    ),
}
CAPTURED = {"host-cmake-passed": CI_RUNS / "passed", "host-cmake-test-failed": CI_RUNS / "failed"}


def zip_bytes(out: Path, overrides: dict[str, bytes] | None = None) -> bytes:
    """The out/ directory zipped as upload-artifact stores it, deterministically."""
    files = {p.relative_to(out).as_posix(): p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file()}
    files.update(overrides or {})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, files[name])
    return buf.getvalue()


def tampered(data: bytes) -> bytes:
    """One byte of the image flipped after the manifest was written."""
    i = len(data) // 2
    return data[:i] + bytes([data[i] ^ 0xFF]) + data[i + 1:]


def build() -> dict[Path, bytes]:
    out: dict[Path, bytes] = {}

    def put_report(name: str, report: dict) -> None:
        out[FIXTURES / "reports" / f"{name}.json"] = (json.dumps(report, indent=2) + "\n").encode()

    for name, src in CAPTURED.items():
        put_report(name, json.loads((src / "report.json").read_text()))
        out[FIXTURES / "artifacts" / f"{name}.zip"] = zip_bytes(src)
    passed = CI_RUNS / "passed"
    out[FIXTURES / "artifacts" / "host-cmake-tampered.zip"] = zip_bytes(
        passed, {"artifacts/firmware.bin": tampered((passed / "artifacts" / "firmware.bin").read_bytes())}
    )

    for name, spec in ASSEMBLED.items():
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = spec.out_dir(Path(tmp))
            report = spec.report(out_dir)
            put_report(name, report)
            if spec.artifact:
                out[FIXTURES / "artifacts" / f"{name}.zip"] = zip_bytes(out_dir)

    for src in sorted((BACKEND_FIXTURES / "github").glob("*.json")):
        out[FIXTURES / "github" / src.name] = src.read_bytes()

    gates = (REPO / ".siliconflow" / "gates.yml").read_text()
    out[FIXTURES / "gates" / "gates.yml"] = gates.encode()
    lowered = gates.replace(
        "{threshold: 40960, profile: host-cmake, requires_stage: package}",
        "{threshold: 20480, profile: host-cmake, requires_stage: package}",
    )
    if lowered == gates:
        raise SystemExit("build_fixtures.py: .siliconflow/gates.yml changed; update the lowered limit")
    out[FIXTURES / "gates" / "gates-text-limit-lowered.yml"] = (
        "# Demo: the host-cmake text limit lowered below the build's size (README demo step 5).\n" + lowered
    ).encode()
    return out


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--check", action="store_true", help="only check that the fixtures are up to date")
    args = p.parse_args(argv)
    wanted = build()
    stale = [path for path, data in wanted.items() if not path.is_file() or path.read_bytes() != data]
    if args.check:
        for path in stale:
            print(f"out of date: {path.relative_to(REPO)}", file=sys.stderr)
        return 1 if stale else 0
    for path in stale:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(wanted[path])
        print(f"wrote {path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
