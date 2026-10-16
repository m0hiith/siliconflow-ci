# Recorded CI runs

Two real runs of the `host-cmake` job, used by `tests/test_build_details.py`
(which also generates the frontend's Playwright fixtures from them). Nothing
here is hand-written or edited.

Captured 2026-10-15 by running the steps of `.github/workflows/firmware-ci.yml`
(`ci/stage.sh …`, then `ci/report.py`) in the `sfci-toolchain` image, in an
Actions-shaped workspace (`/__w/fw/fw`, `GITHUB_REPOSITORY=acme/fw`), against a
two-commit history of this repository's `ci/`, `firmware/` and `.siliconflow/`:

| Directory | Commit | Run id | Result |
| --- | --- | --- | --- |
| `passed/` | `3c9fc2af798c6d5725210693151df0d61346eb06` "Add hysteresis to threshold detector" | 30433700 | every stage passes, including the optional `coverage` stage (100% of lines) |
| `failed/` | `41d8e37920445bf2b14232549a04360c622148b8` "Expect a second rising edge in noisy-signal test" | 30433701 | `test_threshold.c:75` expects 2 rising edges instead of 1; `test` fails; `analyze`, `package` and `coverage` do not run |

`failed/`'s commit is the child of `passed/`'s (`git rev-list --count` = 1).
Each directory is the job's `out/` directory as uploaded. `gates.yml` is
`.siliconflow/gates.yml` at both commits.
