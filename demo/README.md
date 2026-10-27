# Demo scenarios and fixtures

Inputs for demo mode ([docs/demo.md](../docs/demo.md)). Demo mode replays
these through the real backend: the same webhook endpoint, run-report
endpoint, worker, `transitions.py`, `classify.py` and gate evaluation that
handle real CI. Nothing here is a result. Statuses, failure classes, hashes and
gate outcomes are all computed by the backend when the inputs are replayed.

```
demo/
  scenarios/<name>.yml        which runs to replay, in order
  fixtures/reports/*.json     run reports, one per kind of CI job outcome
  fixtures/artifacts/*.zip    the job's zipped out/ directory (the Actions artifact)
  fixtures/gates/*.yml        .siliconflow/gates.yml as a scenario commit has it
  fixtures/github/*.json      GitHub's example webhook payloads
  build_fixtures.py           writes everything under fixtures/
```

Rebuild the fixtures after changing their sources, and check them in CI:

```sh
python3 demo/build_fixtures.py          # rewrite
python3 demo/build_fixtures.py --check  # exit 1 if out of date
```

## What is captured and what is synthetic

**Captured** fixtures are copied unchanged from real CI output. Their
provenance is documented in the backend's fixture READMEs.

| Fixture | Source |
| --- | --- |
| `host-cmake-passed` (report + zip) | [backend/tests/fixtures/ci_runs/passed](../backend/tests/fixtures/ci_runs/README.md): a real `host-cmake` run, every stage passed |
| `host-cmake-test-failed` (report + zip) | [backend/tests/fixtures/ci_runs/failed](../backend/tests/fixtures/ci_runs/README.md): a real run whose Unity test failed |
| `github/*.json` | [backend/tests/fixtures/github](../backend/tests/fixtures/github): GitHub's documented example payloads |
| `gates/gates.yml` | this repository's [.siliconflow/gates.yml](../.siliconflow/gates.yml) |

**Synthetic** fixtures are assembled by `build_fixtures.py`. Their reports
are written by the runner's own `ci/report.py`, so they have exactly the shape
a real runner posts.

| Fixture | What is real | What is made up |
| --- | --- | --- |
| `host-cmake-compile-error` | both stage logs are captured tool output ([logs/README](../backend/tests/fixtures/logs/README.md): `clean.configure.log`, `gcc-undeclared.compile.log`) | the stage timings |
| `cortex-m4-passed` | the ELF size and sections are captured `arm-none-eabi-size` output ([ci/tests/fixtures/size](../ci/tests/fixtures/size)) | the stages and timings; no ELF is stored |
| `siwx917-soc-passed` | nothing | the whole run (the Simplicity SDK image is not public, so no real run exists) |
| `hil-no-jlink` | exit code 75 is what [ci/hil/flash.sh](../ci/hil/flash.sh) returns when the probe is not found | the run itself; no board was attached |
| `host-cmake-tampered.zip` | the captured passed zip | one byte of `artifacts/firmware.bin` flipped after the manifest was written |
| `gates/gates-text-limit-lowered.yml` | the real `gates.yml` | the host-cmake `text_bytes_max` lowered from 40960 to 20480 |

**Scenario fields:**

- Every commit SHA except `clean_main`'s is a placeholder: the SHA-1 of a label, not a real commit.
- Commit messages, authors and run ids are invented.
- The `size` values on history's cortex-m4 builds are a synthetic upward trend from the captured 8448 bytes, so Analytics has a trend to show.

When a scenario is replayed, the fixture's identity is rewritten to the
scenario's run: repository, run id, attempt, commit, job, and timestamps
shifted to "now" (or `days_ago`). Nothing that decides an outcome is rewritten.
That covers stage results, exit codes, JUnit, logs, coverage, cppcheck counts
and the artifact bytes.

## Scenarios

| Scenario | Shows |
| --- | --- |
| `clean_main` | passed, every gate passes, artifacts release-eligible |
| `compile_error` | failed, classified `compile_error`, first error at `src/sensor_logic.c:74` |
| `passed_not_releasable` | passed, but `text_bytes_max` fails on the size the server reads from the stored ELF |
| `redelivery` | the same `workflow_run` delivery three times: accepted, duplicate, duplicate |
| `infra_hw` | a passed siwx917-soc build whose HIL run had no J-Link: the `hil` stage fails as `infra_hw`, and the build stays `passed` |
| `tamper` | a re-hash disagrees with the manifest: artifacts rejected, nothing stored, `artifacts_verified` fails |
| `history` | 25 builds (host-cmake and cortex-m4) over five weeks, including one flaky test |

Analytics groups builds by when the server recorded them, not by when the run
ran. So the history scenario's weekly pass-rate chart shows the week it was
replayed in, while durations, the size trend and flaky tests use the replayed
runs as they are.
