"""Release gates: gates.yml parsing, rule evaluation, the ELF text size, and
the evaluate_gates job deciding artifacts.release_eligible."""
import struct
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import func, select

from app.models import (
    Artifact,
    AuditLog,
    Build,
    BuildStatus,
    BuildTrigger,
    GateResult,
    Repository,
    StageStatus,
)
from app.services import gates
from app.services.elf import ElfError, text_bytes
from app.services.gates import Facts, GatesConfigError, evaluate, parse_config, ref_matches
from app.services.github import GitHubApp
from tests import oidc, reports
from tests.conftest import GITHUB_API
from tests.factories import make_project
from tests.github import SHA, drain
from tests.reports import make_run_build, post
from tests.test_artifacts import INSTALLATION, FakeGitHub, sha

ELF_FIXTURE = Path(__file__).parent / "fixtures" / "elf" / "firmware-host-cmake"
# GNU size(1) on this binary (host-cmake, sanitizers on) printed text=30031.
ELF_TEXT_BYTES = 30031
SAMPLE = Path(__file__).resolve().parents[2] / ".siliconflow" / "gates.yml"
GATES_URL = f"{GITHUB_API}/repos/acme/fw/contents/.siliconflow/gates.yml"

PASSING = b"""
version: 1
required_stages: [configure, compile, test, analyze, package]
rules:
  test_failures: 0
  line_coverage_min: {threshold: 80, requires_stage: test}
  analysis_errors_max: {threshold: 0, requires_stage: analyze}
  text_bytes_max: {threshold: 40960, profile: host-cmake, requires_stage: package}
release_refs: [refs/heads/main, refs/tags/v*]
"""


# ------------------------------------------------------------------ config


def test_the_sample_gates_file_is_valid():
    if not SAMPLE.exists():
        pytest.skip("the repository root is not available (test image)")
    config = parse_config(SAMPLE.read_bytes())
    limits = {r.profile: r.threshold for r in config.rules.text_bytes_max}
    assert limits == {"host-cmake": 40960, "cortex-m4": 12288}
    assert ref_matches("refs/heads/main", config.release_refs)


def test_full_config_parses():
    config = parse_config(PASSING)
    assert config.rules.test_failures.threshold == 0
    assert config.rules.line_coverage_min.threshold == 80
    assert config.rules.text_bytes_max[0].requires_stage == "package"
    assert config.release_refs == ["refs/heads/main", "refs/tags/v*"]


def test_minimal_config_has_no_rules_and_no_release_refs():
    config = parse_config(b"version: 1\n")
    assert config.required_stages == [] and config.release_refs == []
    assert config.rules.test_failures is None


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        (b"version: 2\n", "version"),
        (b"version: 1\nsurprise: true\n", "surprise"),
        (b"version: 1\nrules: {test_failure: 0}\n", "rules.test_failure"),  # typo
        (b"version: 1\nrules: {test_failures: -1}\n", "rules.test_failures.threshold"),
        (b"version: 1\nrules: {test_failures: yes}\n", "rules.test_failures.threshold"),  # bool
        (b"version: 1\nrules: {test_failures: '0'}\n", "rules.test_failures.threshold"),
        (b"version: 1\nrules: {line_coverage_min: 101}\n", "line_coverage_min"),
        (b"version: 1\nrules: {text_bytes_max: {threshold: 1, profil: x}}\n", "profil"),
        (b"version: 1\nrules: {text_bytes_max: []}\n", "text_bytes_max"),
        (b"version: 1\nrules: {text_bytes_max: [{threshold: 1, profile: a}, {threshold: 2, profile: a}]}\n",
         "more than one limit"),
        (b"version: 1\nrules: {text_bytes_max: [1, 2]}\n", "more than one limit"),
        (b"version: 1\nrequired_stages: ['../x']\n", "required_stages"),
        (b"version: 1\nrelease_refs: [main]\n", "release_refs"),
        (b"version: 1\nrelease_refs: ['refs/heads/a b']\n", "release_refs"),
        (b"- version: 1\n", "must be a mapping"),
        (b"", "must be a mapping"),
        (b"version: [1\n", "not valid YAML"),
        (b"\xff\xfe", "not UTF-8"),
        (b"a: &x [1]\nb: *x\n", "aliases"),
        (b"version: 1\n#" + b"x" * gates.MAX_CONFIG_BYTES, "larger than"),
    ],
)
def test_invalid_configs_are_refused(raw, fragment):
    with pytest.raises(GatesConfigError) as exc:
        parse_config(raw)
    assert fragment in str(exc.value)


@pytest.mark.parametrize(
    ("ref", "patterns", "matches"),
    [
        ("refs/heads/main", ["refs/heads/main"], True),
        ("refs/heads/main2", ["refs/heads/main"], False),
        ("refs/tags/v1.2.0", ["refs/tags/v*"], True),
        ("refs/tags/v1/rc", ["refs/tags/v*"], False),  # * stays in one segment
        ("refs/heads/release/1.x/hotfix", ["refs/heads/release/**"], True),
        ("refs/heads/release-1", ["refs/heads/release/**"], False),
        ("refs/heads/r1", ["refs/heads/r?"], True),
        ("refs/heads/a.b", ["refs/heads/a?b"], True),
        ("refs/heads/aXb", ["refs/heads/a.b"], False),  # '.' is literal
        ("refs/heads/main", [], False),
    ],
)
def test_ref_globs(ref, patterns, matches):
    assert ref_matches(ref, patterns) is matches


# -------------------------------------------------------------- evaluation

ALL_PASSED = {name: StageStatus.passed for name in ("configure", "compile", "test", "analyze", "package")}


def facts(**overrides) -> Facts:
    values = dict(
        ref="refs/heads/main",
        trigger=BuildTrigger.push,
        profile="host-cmake",
        stages=dict(ALL_PASSED),
        artifacts_state="stored",
        artifact_count=2,
        test_failures=0,
        line_coverage=91.25,
        analysis_errors=0,
        text_bytes=ELF_TEXT_BYTES,
        text_file="firmware",
    )
    values.update(overrides)
    return Facts(**values)


def by_id(results) -> dict:
    return {r.rule_id: r for r in results}


def test_everything_passing_is_eligible():
    results = evaluate(parse_config(PASSING), facts())
    assert [r.rule_id for r in results] == [
        "artifacts_verified",
        "stage:configure",
        "stage:compile",
        "stage:test",
        "stage:analyze",
        "stage:package",
        "test_failures",
        "line_coverage_min",
        "analysis_errors_max",
        "text_bytes_max",
        "release_ref",
    ]
    assert all(r.passed for r in results), [r for r in results if not r.passed]
    text = by_id(results)["text_bytes_max"]
    assert (text.observed, text.threshold) == (ELF_TEXT_BYTES, 40960)


@pytest.mark.parametrize(
    ("overrides", "rule_id", "message"),
    [
        ({"test_failures": 1}, "test_failures", "1 failing test(s), more than the 0 allowed"),
        ({"line_coverage": 79.9}, "line_coverage_min", "line coverage 79.9% is below the 80.0% minimum"),
        ({"analysis_errors": 2}, "analysis_errors_max", "2 analysis error(s), more than the 0 allowed"),
        ({"text_bytes": 40961}, "text_bytes_max", "firmware: 40961 text bytes, over the 40960-byte limit"),
        ({"ref": "refs/heads/develop"}, "release_ref", "refs/heads/develop matches no release_refs pattern"),
        (
            {"trigger": BuildTrigger.pull_request, "ref": "refs/heads/main"},
            "release_ref",
            "pull request builds are never release-eligible",
        ),
        ({"artifacts_state": "rejected", "artifact_count": 0}, "artifacts_verified", "artifacts are rejected with 0 stored"),
        ({"artifact_count": 0}, "artifacts_verified", "artifacts are stored with 0 stored"),
    ],
)
def test_each_failing_check_blocks_release(overrides, rule_id, message):
    results = by_id(evaluate(parse_config(PASSING), facts(**overrides)))
    assert results[rule_id].passed is False
    assert results[rule_id].message == message
    assert [r for r in results.values() if not r.passed] == [results[rule_id]]


def test_thresholds_are_inclusive():
    results = by_id(evaluate(
        parse_config(PASSING), facts(line_coverage=80.0, text_bytes=40960)
    ))
    assert results["line_coverage_min"].passed and results["text_bytes_max"].passed


def test_missing_measurements_fail_closed():
    f = facts(
        test_failures=None,
        line_coverage=None,
        analysis_errors=None,
        text_bytes=None,
        text_file=None,
        missing={"test_failures": "no test cases were recorded", "text_bytes_max": "no ELF artifact to measure"},
    )
    results = by_id(evaluate(parse_config(PASSING), f))
    for rule_id in ("test_failures", "line_coverage_min", "analysis_errors_max", "text_bytes_max"):
        assert results[rule_id].passed is False
        assert results[rule_id].observed is None
    assert results["test_failures"].message == "no test cases were recorded"
    assert results["text_bytes_max"].message == "no ELF artifact to measure"
    assert results["line_coverage_min"].message == "not measured"


def test_a_rule_fails_when_its_stage_did_not_pass():
    stages = dict(ALL_PASSED, analyze=StageStatus.failed)
    del stages["test"]
    results = by_id(evaluate(parse_config(PASSING), facts(stages=stages)))
    assert results["line_coverage_min"].message == "requires stage test: stage test was not reported"
    assert results["line_coverage_min"].observed == 91.25  # still recorded
    assert results["analysis_errors_max"].message == "requires stage analyze: stage analyze failed"
    assert results["stage:analyze"].passed is False
    assert results["stage:analyze"].observed == "failed"
    assert results["stage:test"].observed is None


def test_text_size_rule_for_another_profile_does_not_apply():
    results = by_id(evaluate(parse_config(PASSING), facts(profile="siwx917", text_bytes=None)))
    rule = results["text_bytes_max"]
    assert rule.passed is True and rule.observed is None
    assert rule.message == "applies to profile host-cmake only; this build is siwx917"


PER_PROFILE = b"""
version: 1
rules:
  text_bytes_max:
    - {threshold: 40960, profile: host-cmake}
    - {threshold: 12288, profile: cortex-m4, requires_stage: package}
"""


def test_text_size_limit_is_chosen_by_profile():
    config = parse_config(PER_PROFILE)
    host = by_id(evaluate(config, facts(text_bytes=30031)))["text_bytes_max"]
    assert (host.passed, host.threshold) == (True, 40960)
    m4 = by_id(evaluate(config, facts(profile="cortex-m4", text_bytes=12289, text_file="firmware.elf")))
    assert m4["text_bytes_max"].passed is False
    assert m4["text_bytes_max"].message == "firmware.elf: 12289 text bytes, over the 12288-byte limit"


def test_text_size_limit_for_no_listed_profile_does_not_apply():
    rule = by_id(evaluate(parse_config(PER_PROFILE), facts(profile="siwx917")))["text_bytes_max"]
    assert (rule.passed, rule.observed, rule.threshold) == (True, None, None)
    assert rule.message == "applies to profile host-cmake, cortex-m4 only; this build is siwx917"


def test_a_limit_without_profile_covers_unlisted_profiles():
    config = parse_config(b"version: 1\nrules: {text_bytes_max: [{threshold: 100}, {threshold: 9, profile: m4}]}\n")
    assert by_id(evaluate(config, facts(profile="host-cmake", text_bytes=50)))["text_bytes_max"].threshold == 100
    assert by_id(evaluate(config, facts(profile="m4", text_bytes=50)))["text_bytes_max"].passed is False


def test_no_release_refs_means_never_eligible():
    results = by_id(evaluate(parse_config(b"version: 1\n"), facts()))
    assert results["release_ref"].passed is False
    assert set(results) == {"artifacts_verified", "release_ref"}


# --------------------------------------------------------------------- ELF


def elf32_be(sections: list[tuple[int, int, int]], *, truncate_at: int | None = None) -> bytes:
    """A big-endian ELF32 file with the given (type, flags, size) sections
    after the null section, contents zeroed."""
    ehsize, shentsize = 52, 40
    body = b"".join(b"\0" * size if stype != 8 else b"" for stype, _, size in sections)
    shoff = ehsize + len(body)
    ident = b"\x7fELF" + bytes([1, 2, 1]) + b"\0" * 9
    header = ident + struct.pack(">HHIIIIIHHHHHH", 2, 40, 1, 0, 0, shoff, 0, ehsize, 0, 0, shentsize, len(sections) + 1, 0)
    table = struct.pack(">IIIIIIIIII", *[0] * 10)
    offset = ehsize
    for stype, flags, size in sections:
        table += struct.pack(">IIIIIIIIII", 0, stype, flags, 0, offset, size, 0, 0, 4, 0)
        offset += 0 if stype == 8 else size
    data = header + body + table
    return data[:truncate_at] if truncate_at is not None else data


def test_text_bytes_matches_gnu_size_for_the_real_firmware():
    assert text_bytes(ELF_FIXTURE) == ELF_TEXT_BYTES


def test_text_bytes_counts_read_only_allocated_sections(tmp_path):
    alloc, alloc_exec, write = 0x2, 0x6, 0x1
    path = tmp_path / "fw.elf"
    path.write_bytes(elf32_be([
        (1, alloc_exec, 1000),     # .text
        (1, alloc, 200),           # .rodata
        (1, alloc | write, 300),   # .data: not text
        (8, alloc | write, 4000),  # .bss: not text
        (1, 0, 50),                # .comment: not allocated
    ]))
    assert text_bytes(path) == 1200


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not an elf at all",
        b"\x7fELF" + bytes([3, 1, 1]) + b"\0" * 9 + b"\0" * 40,  # unknown class
        elf32_be([(1, 0x6, 10)], truncate_at=30),  # header cut short
        elf32_be([(1, 0x6, 10)], truncate_at=80),  # section table cut short
    ],
)
def test_malformed_elves_are_refused(tmp_path, data):
    path = tmp_path / "bad"
    path.write_bytes(data)
    with pytest.raises(ElfError):
        text_bytes(path)


# ---------------------------------------------------------------- the job


@pytest.fixture(scope="module")
def key():
    return oidc.new_key()


@pytest.fixture
def mock(key):
    with respx.mock(assert_all_called=False) as router:
        router.get(oidc.JWKS_URL).respond(json=oidc.jwks((key, oidc.KID)))
        yield router


@pytest.fixture
def gh(mock) -> FakeGitHub:
    fake = FakeGitHub(mock)
    elf = ELF_FIXTURE.read_bytes()
    fake.zip = reports.artifact_zip({"artifacts/firmware": elf})
    return fake


@pytest.fixture
def gates_file(mock):
    """gates.yml at the build's commit. Set .content to None for a 404."""

    class GatesFile:
        content: bytes | None = PASSING

    state = GatesFile()

    def respond(request):
        assert request.headers["accept"] == "application/vnd.github.raw+json"
        if state.content is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, content=state.content)

    state.route = mock.get(GATES_URL, params={"ref": SHA}).mock(side_effect=respond)
    return state


@pytest.fixture
async def github(settings):
    client = GitHubApp(settings)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def project(session):
    project = await make_project(session, "fw")
    repo = await session.get_one(Repository, project.repositories[0].id)
    repo.gh_installation_id = INSTALLATION
    await session.commit()
    return project


def elf_report(**overrides):
    """The real report, with its firmware entry describing the real ELF."""
    body = reports.report(**overrides)
    elf = ELF_FIXTURE.read_bytes()
    for f in body["manifest"]["files"]:
        if f["file"] == "firmware":
            f.update(size=len(elf), sha256=sha(elf))
    return body


async def run_build(client, session, sessionmaker, settings, github, key, project, body=None, **build_fields):
    """A build whose run concluded with build_fields' gh_conclusion (default
    success), its report posted and every job drained."""
    build_fields.setdefault("gh_conclusion", "success")
    build = await make_run_build(session, project, **build_fields)
    r = await post(client, body or elf_report(), reports.token(key))
    assert r.status_code == 202, r.text
    await drain(sessionmaker, settings, github)
    session.expunge_all()
    return await session.get_one(Build, build.id)


async def results_of(session, build_id) -> dict[str, GateResult]:
    rows = await session.scalars(select(GateResult).where(GateResult.build_id == build_id))
    return {r.rule_id: r for r in rows}


async def eligible(session, build_id) -> list[bool]:
    return list(await session.scalars(
        select(Artifact.release_eligible).where(Artifact.build_id == build_id).order_by(Artifact.file_name)
    ))


async def test_passed_build_on_a_release_ref_is_eligible(
    client, session, sessionmaker, settings, github, key, project, gh, gates_file
):
    build = await run_build(client, session, sessionmaker, settings, github, key, project)
    assert build.status is BuildStatus.passed

    results = await results_of(session, build.id)
    assert all(r.passed for r in results.values()), {k: r.message for k, r in results.items() if not r.passed}
    assert results["test_failures"].observed == 0
    assert results["line_coverage_min"].observed == 91.25
    assert results["line_coverage_min"].threshold == 80
    assert results["text_bytes_max"].observed == ELF_TEXT_BYTES
    assert results["release_ref"].threshold == ["refs/heads/main", "refs/tags/v*"]
    assert await eligible(session, build.id) == [True, True]

    audit = (await session.scalars(select(AuditLog).where(AuditLog.action == "gates.evaluated"))).one()
    assert audit.details == {"release_eligible": True, "failed": []}
    # Queued twice (on passing and after ingestion), evaluated once.
    assert gates_file.route.call_count == 1


async def test_one_failing_rule_blocks_every_artifact(
    client, session, sessionmaker, settings, github, key, project, gh, gates_file
):
    gates_file.content = PASSING.replace(b"line_coverage_min: {threshold: 80", b"line_coverage_min: {threshold: 95")
    build = await run_build(client, session, sessionmaker, settings, github, key, project)

    results = await results_of(session, build.id)
    assert [k for k, r in results.items() if not r.passed] == ["line_coverage_min"]
    assert results["line_coverage_min"].message == "line coverage 91.25% is below the 95.0% minimum"
    assert await eligible(session, build.id) == [False, False]


async def test_a_branch_outside_release_refs_is_not_eligible(
    client, session, sessionmaker, settings, github, key, project, gh, gates_file
):
    build = await run_build(client, session, sessionmaker, settings, github, key, project, branch="develop")
    results = await results_of(session, build.id)
    assert results["release_ref"].passed is False
    assert results["release_ref"].observed == "refs/heads/develop"
    assert await eligible(session, build.id) == [False, False]


async def test_no_gates_file_fails_closed(
    client, session, sessionmaker, settings, github, key, project, gh, gates_file
):
    gates_file.content = None
    build = await run_build(client, session, sessionmaker, settings, github, key, project)
    results = await results_of(session, build.id)
    assert list(results) == ["gates_config"]
    assert results["gates_config"].passed is False
    assert results["gates_config"].message == f"no .siliconflow/gates.yml at commit {SHA[:12]}"
    assert await eligible(session, build.id) == [False, False]


async def test_an_invalid_gates_file_fails_closed(
    client, session, sessionmaker, settings, github, key, project, gh, gates_file
):
    gates_file.content = b"version: 1\nrules: {test_failures: lots}\n"
    build = await run_build(client, session, sessionmaker, settings, github, key, project)
    results = await results_of(session, build.id)
    assert list(results) == ["gates_config"]
    assert "rules.test_failures.threshold" in results["gates_config"].message
    assert await eligible(session, build.id) == [False, False]


async def test_rejected_artifacts_fail_the_gates(
    client, session, sessionmaker, settings, github, key, project, gh, gates_file
):
    gh.zip = reports.artifact_zip({"artifacts/firmware": b"tampered"})
    build = await run_build(client, session, sessionmaker, settings, github, key, project)
    assert build.status is BuildStatus.passed  # ingestion never changes status
    results = await results_of(session, build.id)
    failed = {k for k, r in results.items() if not r.passed}
    assert failed == {"artifacts_verified", "test_failures", "text_bytes_max"}
    assert results["test_failures"].message == "no verified test results (artifacts are rejected)"
    assert await eligible(session, build.id) == []


async def test_a_failed_build_is_never_evaluated(
    client, session, sessionmaker, settings, github, key, project, gh, gates_file
):
    body = reports.with_stage(elf_report(), "test", "failed")
    build = await run_build(
        client, session, sessionmaker, settings, github, key, project, body=body, gh_conclusion="failure"
    )
    assert build.status is BuildStatus.failed
    assert await results_of(session, build.id) == {}
    assert await eligible(session, build.id) == [False, False]  # ingested, never eligible
    assert gates_file.route.call_count == 0


@pytest.mark.parametrize(
    "status",
    [BuildStatus.running, BuildStatus.cancelled, BuildStatus.timed_out, BuildStatus.errored],
)
async def test_only_passed_builds_are_evaluated(session, sessionmaker, settings, github, project, gates_file, status):
    build = await make_run_build(session, project, status=status)
    async with sessionmaker() as s, s.begin():
        await gates.evaluate_build(s, github, settings, build.id)
    assert await results_of(session, build.id) == {}
    assert gates_file.route.call_count == 0


async def test_gates_wait_for_ingestion(
    client, session, sessionmaker, settings, github, key, project, gh, gates_file
):
    build = await make_run_build(session, project, gh_conclusion="success")
    assert (await post(client, elf_report(), reports.token(key))).status_code == 202
    async with sessionmaker() as s, s.begin():
        assert (await s.get_one(Build, build.id)).status is BuildStatus.passed
        await gates.evaluate_build(s, github, settings, build.id)  # artifacts still pending
    assert await results_of(session, build.id) == {}

    await drain(sessionmaker, settings, github)
    assert len(await results_of(session, build.id)) == 11
    async with sessionmaker() as s, s.begin():  # a late duplicate job changes nothing
        await gates.evaluate_build(s, github, settings, build.id)
    assert await session.scalar(select(func.count()).select_from(GateResult)) == 11


# ------------------------------------------- the run report cannot set eligibility


@pytest.mark.parametrize(
    "claim",
    [
        lambda b: b.update(release_eligible=True),
        lambda b: b["manifest"]["files"][0].update(release_eligible=True),
        lambda b: b.update(gates={"passed": True}),
        lambda b: b.update(gate_results=[{"rule_id": "release_ref", "passed": True}]),
    ],
)
async def test_a_run_report_cannot_claim_release_eligibility(
    client, session, sessionmaker, settings, github, key, project, gh, gates_file, claim
):
    build = await make_run_build(session, project, gh_conclusion="success")
    body = elf_report()
    claim(body)
    r = await post(client, body, reports.token(key))
    assert r.status_code == 422
    await drain(sessionmaker, settings, github)
    session.expunge_all()
    assert (await session.get_one(Build, build.id)).status is BuildStatus.running
    assert await session.scalar(select(func.count()).select_from(Artifact)) == 0
    assert await session.scalar(select(func.count()).select_from(GateResult)) == 0


async def test_a_flawless_report_is_still_not_eligible_without_gates(
    client, session, sessionmaker, settings, github, key, project, gh, gates_file
):
    """Everything the runner can say is green, but only the backend's gates
    decide eligibility."""
    gates_file.content = PASSING.replace(b"release_refs: [refs/heads/main, refs/tags/v*]", b"")
    build = await run_build(client, session, sessionmaker, settings, github, key, project)
    assert build.status is BuildStatus.passed
    results = await results_of(session, build.id)
    assert [k for k, r in results.items() if not r.passed] == ["release_ref"]
    assert await eligible(session, build.id) == [False, False]
