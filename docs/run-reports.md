# Run reports

At the end of every firmware job, the runner sends the backend a **run
report**: a JSON document (schema `sfci.run-report/1`) that records what the
job did. The runner only reports facts. The backend authenticates the report,
checks it against what it already knows, and then decides the build's status
itself.

```
firmware-ci.yml (host-cmake job)                 backend
────────────────────────────────                 ───────
ci/stage.sh ...      → out/stages.jsonl
ci/report.py         → out/report.json
upload-artifact      → Actions artifact sfci-out-host-cmake-attempt-N
ci/post_report.sh    ── OIDC JWT + report ──▶ POST /api/v1/runs/report
                                                ├─ stores stages, run_reports row
                                                ├─ queues ingest_artifacts job
                                                └─ settles the build if the run has concluded
                                              worker: ingest_artifacts
                                                ├─ downloads the artifact zip (GitHub App)
                                                ├─ re-hashes every file and checks the manifest
                                                ├─ stores files under ARTIFACT_ROOT/sha256/ab/cd/<hash>
                                                ├─ parses JUnit into test_cases
                                                └─ redacts, classifies and stores stage logs
```

## Runner side

| Script | Does |
| --- | --- |
| `ci/coverage.sh` | The optional `coverage` stage: builds `firmware/` again with `-DSFCI_COVERAGE=ON` in its own directory (the packaged build stays uninstrumented), runs the tests and writes gcovr's Cobertura report to `out/coverage/coverage.xml`. It runs after `package`, so a failure cannot skip a required stage; the `line_coverage_min` gate fails when no coverage is reported. |
| `ci/report.py` | Reads `out/` and writes `out/report.json`. A required stage (`--required`) with no line in `stages.jsonl` never ran, so it is reported as `skipped`. Toolchain versions come from `gcc --version`, `arm-none-eabi-gcc --version` and `cmake --version`, and `sdk` is `null`; with `--versions <file>` both `sdk` and `toolchain` come from that file instead (see *SiWx917 SoC profile* below). Coverage comes from `out/coverage/coverage.xml` and cppcheck counts from `out/analysis/cppcheck-summary.json`. JUnit files are listed with their size and SHA-256, and the artifact manifest is included. Any input that is missing becomes `null`. |
| `ci/size.py` | The `size` stage of the cortex-m4 workflow. Runs `arm-none-eabi-size -A -d` for the section list and `-B -d` for the Berkeley text/data/bss totals (`-A` prints no section flags, so it cannot classify sections), and writes `out/size/size.json`. `ci/report.py` puts it in the report as `size`; it is `null` when the file is missing or malformed. |
| `ci/post_report.sh` | Requests an OIDC token with audience `siliconflow` and POSTs the report. It runs as the final step with `if: always()`. It retries `404`, `429`, `5xx` and connection failures with backoff. A `409` (build already finished) is reported as a warning. The token is masked and never printed. |

### Setup

- The job needs `permissions: id-token: write`.
- Set the repository variable **`SFCI_API_URL`** to the backend's base URL,
  for example `https://sfci.example.com`. If it is unset, the step warns and
  sends nothing.
- Fork pull requests get no OIDC token. Their builds end up `errored` (see
  below), because no report can arrive.

## Backend side

### Authentication

`POST /api/v1/runs/report` needs `Authorization: Bearer <GitHub OIDC JWT>`.
The token is checked before the body is validated:

- The signature is verified (RS256 only) against GitHub's JWKS. Keys are
  cached for `SFCI_OIDC_JWKS_CACHE_S`. A token with an unknown key id triggers
  at most one refetch per minute.
- `iss` must be `https://token.actions.githubusercontent.com`, `aud` must be
  `SFCI_OIDC_AUDIENCE`, and `exp`, `iat` and `nbf` are checked with 60 s of
  leeway.
- `workflow_ref` must point at a firmware workflow in the same repository:
  `SFCI_GITHUB_WORKFLOW_PATH`, or one listed in `SFCI_GITHUB_EXTRA_WORKFLOWS`.
- The body's `github.{repository, run_id, run_attempt, sha}` must equal the
  token's claims.
- The build is looked up by `(run_id, run_attempt)` from the token. Its
  repository must match by name, and also by numeric id once that id is
  known. For push and manual builds, the token's `sha` must equal the build's
  commit. Pull request jobs run on GitHub's merge commit, which is not the PR
  head the build records, so for those builds the token's `ref`
  (`refs/pull/N/merge`) must equal the build's ref.

| Status | Meaning | `post_report.sh` |
| --- | --- | --- |
| 202 | Accepted | done |
| 200 | Identical report already recorded | done |
| 401 | Missing, forged or expired token | fails |
| 403 | Token and report or build disagree | fails |
| 404 | No build for this run yet (webhook still in flight) | retries |
| 409 | Build already finished, or a *different* report was already recorded | warns |
| 422 | Body does not match `sfci.run-report/1` | fails |
| 503 | GitHub's JWKS unreachable | retries |

### Profile

A build is created from the `workflow_run` webhook with its workflow's name as
its profile (`firmware-ci`), since one run can have several jobs. The accepted
report's `github.job` (e.g. `host-cmake`) then becomes the profile: it is the
job that ran the stages, produced the artifacts and is gated, and it is what
`profile` in `.siliconflow/gates.yml` refers to. A missing job, or one with
characters outside `A-Z a-z 0-9 . _ -`, leaves the profile unchanged.

### Firmware workflows and profiles

Each firmware workflow is tracked as its own builds, one per run attempt, with
one run report each. A second profile is therefore a second workflow, not a
second job in `firmware-ci.yml`, whose report would be refused with `409`.

| Workflow | Job (profile) | Required stages | Setting |
| --- | --- | --- | --- |
| `firmware-ci.yml` | `host-cmake` | `configure,compile,test,analyze,package` | `SFCI_GITHUB_WORKFLOW_PATH`, `SFCI_REQUIRED_STAGES` |
| `firmware-ci-cortex-m4.yml` | `cortex-m4` | `configure,compile,package,size` | `SFCI_GITHUB_EXTRA_WORKFLOWS` |
| `firmware-ci-siwx917-soc.yml` | `siwx917-soc` | `generate,compile,package` | `SFCI_GITHUB_EXTRA_WORKFLOWS` |

`SFCI_GITHUB_EXTRA_WORKFLOWS` is JSON mapping workflow paths to their required
stages, and replaces the default shown above:
`{".github/workflows/firmware-ci-cortex-m4.yml": ["configure", "compile", "package", "size"], ".github/workflows/firmware-ci-siwx917-soc.yml": ["generate", "compile", "package"]}`.
Manual builds always dispatch `SFCI_GITHUB_WORKFLOW_PATH`.

`hil.yml` is not a firmware workflow and creates no builds. Its report is a
*linked report* (`profile` and `build_id` set) that adds a `hil` stage to the
siwx917-soc build it tested; see [hil.md](hil.md).

The cortex-m4 profile cross-compiles `firmware/` with
`firmware/cmake/arm-none-eabi.cmake` (Cortex-M4F, hard float, newlib-nano,
`--gc-sections`) and links it with a minimal startup file and linker script
for a generic part (`firmware/target/`). It proves the code builds for the
target and measures its size. It runs no tests and targets no real silicon,
so it fails the `stage:test` and test gates and is never release-eligible.

### SiWx917 SoC profile

`firmware/siwx917/` holds the only committed inputs: `siwx917_soc.slcp`
(started from the WiSeConnect example `sl_si91x_blinky`), `app.c` and
`hal_siwx917.c`. The project reuses `firmware/src/sensor_logic.c` and
implements `hal.h` with the SDK's sleeptimer (`hal_now_ms`), BTN0's GPIO
(`hal_read_button`) and the on-chip BJT temperature sensor, in 0.1 °C
(`hal_read_sensor`). `ci/siwx917.sh` runs its stages:

| Stage | Does |
| --- | --- |
| `generate` | `slc generate` for board `brd4338a` into `out/gen` (git-ignored, and left out of the uploaded artifact). `SFCI_SDK_PACKAGE_PATH` overrides the SDKs slc is configured with. |
| `compile` | Builds the generated makefile project (`release`). The SDK's post-build step (Simplicity Commander) writes the `.rps` flash image and converts it to `.hex`, `.s37` and `_isp.bin`. |
| `package` | Packages the ELF (`siwx917_soc.out`), `.bin` and `.map`, which must exist, and whichever of `.rps`, `_isp.bin`, `.hex` and `.s37` the post-build produced. `ci/package.sh` keeps the SDK's `.bin` rather than writing its own. |

The job runs in a private image pinned by digest, so the workflow runs only on
pushes to `main` and on tags; it has no `pull_request` trigger. The image
declares its versions in `/opt/sfci/versions.json`, which `ci/report.py
--versions` copies into the report:

```json
{"sdk": {"simplicity_sdk": "2026.6.1", "wiseconnect": "4.1.1"},
 "toolchain": {"slc_cli": "6.0.23", "arm-none-eabi-gcc": "14.2.1"}}
```

Each section must be a non-empty object of name to version strings; a missing
or malformed file or section is reported as `null` (`sdk`) or `{}`
(`toolchain`), with a warning. `GET /api/v1/builds/{id}` returns both, and
the build details page shows them as chips in its header and in the artifact
provenance panel. Like cortex-m4, the profile runs no tests, so it fails the
test gates and is never release-eligible.

### Required stages

The required stages of the token's workflow (see above) are the backend's own
list. A report can mark extra stages as required, but it
cannot make one of these optional. A required stage that the report leaves
out is stored as `skipped`.

### Final status

The decision is made in `app/services/outcome.py`, and status changes go
through `services/transitions.py`. It applies only to runs that GitHub
concludes `success` or `failure`. The webhook alone settles the others:
`cancelled`, `skipped` and `stale` become **cancelled**, `timed_out` becomes
**timed_out**, and `startup_failure`, `neutral` and any conclusion GitHub
adds later become **errored**. `action_required` (a run waiting for approval)
leaves the build queued; once approved, the same run attempt continues.

| Conclusion | Report | Required stages | Build |
| --- | --- | --- | --- |
| success | arrived | all passed | **passed** |
| success | arrived | any failed or skipped | failed |
| failure | arrived | any | failed |
| success or failure | none after `SFCI_REPORT_DEADLINE_S` (600 s) | — | **errored** |

The report is posted from inside the job, so it usually arrives before the
`workflow_run completed` webhook. Whichever arrives second settles the build.
Both take the run's advisory lock, so neither can miss the other. If the
report has not arrived when the run completes, a `report_deadline` job is
queued for 10 minutes later.

A run that concludes `failure` with no report is also `errored`. The report
step runs with `if: always()`, so a missing report means the infrastructure
failed, and nothing says the firmware did.

One exception: "Re-run failed jobs" reuses the jobs that succeeded, so if
only another job failed, the job that posts the report does not run again.
At its deadline, a re-run attempt with no report of its own inherits the
latest earlier attempt's report when every required stage in it passed (an
`run_report.inherited` audit entry records which). Its artifacts are fetched
again from the same run.

The worker's reconciler (`services/reconciler.py`) also finishes builds that
nothing else would: one whose deadline job failed for good is settled as that
job would have, and a manual build that no run was bound to within
`SFCI_BUILD_TIMEOUT_S` + `SFCI_RECONCILE_GRACE_S` is errored.

`ci/post_report.sh` keeps retrying a `404` (no build yet) for about two
minutes, so a report that overtakes a backlog of webhooks still gets in.

### Size

A report's `size` (from `ci/size.py`) is stored in `build_sizes`, one row per
build: the ELF's name, text/data/bss bytes and its `size -A` sections. It is
the runner's measurement and is only displayed and compared. The
`text_bytes_max` gate measures the stored ELF itself.

| Endpoint | Returns |
| --- | --- |
| `GET /api/v1/projects/{id}/sizes?branch=&profile=&limit=` | Reported sizes of the project's builds, newest first, each with its build's number, status, profile, branch and commit. |
| `GET /api/v1/builds/{id}/size` | The build's size, and the size of the latest passing build before it on the repository's default branch with the same profile (the Size tab shows the delta). Pull request builds are compared with the default branch too. |

### Artifacts

The `ingest_artifacts` job runs for every report that names an artifact,
with or without a manifest or JUnit files: a build that failed to compile has
neither, but its stage logs are in the zip. It downloads the artifact through
the GitHub App. GitHub answers with a redirect to blob storage, and the
download from there sends no `Authorization` header. The job then verifies the
zip against the report, **not** against the `manifest.json` inside the zip:

- every manifest file must exist under `artifacts/` with the same size and
  SHA-256;
- no unlisted files may exist under `artifacts/`;
- every listed JUnit file must match its size and SHA-256.

Any mismatch rejects the whole set. Nothing is stored,
`run_reports.artifacts_state` becomes `rejected` with the problems listed, and
an `artifacts.rejected` row is written to `audit_log`. An artifact that has
expired or is over `SFCI_ARTIFACT_MAX_DOWNLOAD_BYTES` gives `unavailable`.
Ingestion never changes the build's status.

Verified files are stored read-only, once per content, at
`$SFCI_ARTIFACT_ROOT/sha256/ab/cd/<sha256>`.
`GET /api/v1/artifacts/{id}/download` streams a stored file with
`X-Checksum-SHA256: <hex>`, the hash the server computed.

### Stage logs

`ci/stage.sh` writes each stage's output to `out/logs/<stage>.log`. Once the
zip has passed verification, ingestion takes the log of every stage the build
has (`app/services/stage_logs.py`):

- A log over `SFCI_STAGE_LOG_MAX_BYTES` (16 MiB) keeps its first and last
  halves, cut at line ends, with a `[siliconflow: N bytes omitted ...]` line
  between them.
- Strings that look like GitHub tokens (`ghp_`, `gho_`, `ghu_`, `ghs_`,
  `ghr_`, `github_pat_`) or JWTs become `[REDACTED:github-token]` or
  `[REDACTED:jwt]` before the log is indexed or stored.
- The log is gzipped and stored content-addressed like an artifact.
  `build_stages.log_key` holds its key. `build_stages.log_index` holds the
  error and warning line numbers, the failure class and the first error.

Logs are not in the report and are not hashed by the runner. Like stage
statuses, they are the runner's account and are never used for gating. A
rejected zip stores no logs.

`app/services/classify.py` classifies each log with an ordered table of
regular expressions. The first rule in the table that matches any line
decides the class:

| Order | Class | Matches (real samples in `backend/tests/fixtures/logs`) |
| --- | --- | --- |
| 1 | `infra_hw` | OpenOCD cannot find the CMSIS-DAP or J-Link probe |
| 2 | `infra_ci` | `No space left on device`, OOM-killed compiler, `Could not resolve host` |
| 3 | `size_overflow` | ld `region ... overflowed`, `section ... will not fit in region` |
| 4 | `link_error` | `undefined reference`, `multiple definition`, `ld returned N exit status` |
| 5 | `test_failure` | Unity `:FAIL`, sanitizer `runtime error`, ctest `The following tests FAILED` |
| 6 | `analysis` | `ci/cppcheck_gate.py` failure, cppcheck `error: ... [checkId]` |
| 7 | `compile_error` | gcc `file:line:col: error:`, `-Werror`, `CMake Error` |

A stage known only by its exit code, such as the `hil` stage of a linked
report, is `infra_hw` when that code is 75 (the code the `ci/hil` scripts use
for probe, serial port and flash verify failures).

Infrastructure comes first because a full disk or a missing probe also prints
compiler- or test-shaped errors. `builds.failure_class` is the class of the
build's first failed required stage that has a classified log. It describes
why the build failed and never changes the build's status.

The first error is the first error line that names a source location, giving
its file, line, column and message. For infrastructure classes it is the line
that decided the class instead.

| Endpoint | Returns |
| --- | --- |
| `GET /api/v1/builds/{id}/logs/{stage}?offset=&limit=` | `lines[offset:offset+limit]` of the redacted log (`limit` ≤ 5000, default 1000), `total_lines`, and the whole log's index: `errors` and `warnings` (1-based line numbers), their counts, `failure_class` and `first_error`. To start at error line *n*, pass `offset=n-1`. Returns 404 for an unknown stage or when no log is stored. |
| `GET /api/v1/builds/{id}/previous-passing` | The last passing build before this one on the same repository, ref and profile, and `compare_url`, a GitHub compare view from that build's commit to this one's. `compare_url` is `null` when either commit is unknown or both are the same commit. Both fields are `null` when there is no such build. |
