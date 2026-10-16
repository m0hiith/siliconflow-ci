# Release gates

A build that **passed** is not automatically releasable. Its artifacts become
`release_eligible` only when every check in the repository's release policy
passes and the build ran on a release ref. Only the backend decides this. The
run report has no field for it, and a report that adds one is refused with
`422`.

The policy is `.siliconflow/gates.yml`, read from the repository **at the
build's commit**. See the [sample](../.siliconflow/gates.yml).

```yaml
version: 1
required_stages: [configure, compile, test, analyze, package]
rules:
  test_failures:       {threshold: 0,     requires_stage: test}
  line_coverage_min:   {threshold: 80,    requires_stage: test}
  analysis_errors_max: {threshold: 0,     requires_stage: analyze}
  text_bytes_max:
    - {threshold: 40960, requires_stage: package, profile: host-cmake}
    - {threshold: 12288, requires_stage: package, profile: cortex-m4}
release_refs: [refs/heads/main, refs/tags/v*]
```

Every rule is optional. `rule: N` is short for `rule: {threshold: N}`.
Unknown keys, wrong types (including YAML `yes` for a number) and YAML aliases
are errors.

## Checks

Each check writes one `gate_results` row (`rule_id`, `passed`, `observed`,
`threshold`, `message`).

| rule_id | Passes when | Observed value comes from |
| --- | --- | --- |
| `artifacts_verified` | the artifact set was downloaded, re-hashed and stored | the ingestion job (always checked) |
| `stage:<name>` | that stage passed (one row per `required_stages` entry) | `build_stages` |
| `test_failures` | failed + errored test cases ≤ threshold | `test_cases`, parsed from server-verified JUnit files |
| `line_coverage_min` | line coverage ≥ threshold | the run report's `coverage.line_percent` |
| `analysis_errors_max` | cppcheck errors ≤ threshold | the run report's `cppcheck.error` |
| `text_bytes_max` | text bytes of the largest ELF artifact ≤ threshold | computed by the server from the stored ELF |
| `release_ref` | the build's ref matches a `release_refs` pattern | `builds.ref` (always checked) |

- **`requires_stage`.** The rule fails if the named stage did not pass. The
  observed value is still recorded.
- **`profile`.** `text_bytes_max` is one limit or a list of limits, at most
  one per `profile` and at most one without a profile. A build is checked
  against the limit naming its profile, else the limit with no profile. If
  neither exists, the rule passes and its message says it does not apply. A build's profile is the job named in its run report
  (`github.job`, e.g. `host-cmake`); it is the workflow's name only until the
  report arrives, and gates are evaluated after that.
- **Text bytes** use the Berkeley figure that GNU `size` prints: allocated,
  read-only sections that take file space. The server reads only the ELF
  section headers. Nothing is executed.
- **`release_refs`** are full refs. `*` and `?` stay within one path segment,
  and `**` crosses segments. With no patterns, nothing is ever eligible.

## Fail-closed rules

- A missing measurement fails its rule. Examples: no coverage in the report,
  JUnit files that were rejected or failed to parse, zero test cases, or no
  readable ELF artifact.
- A missing, unreadable or invalid `gates.yml` writes a single failed
  `gates_config` row.
- Pull request builds are never eligible, because the PR author controls
  their `gates.yml`.
- Builds that end `failed`, `cancelled`, `timed_out` or `errored` are never
  evaluated, so their artifacts stay ineligible.

Because the policy is read at the build's commit, anyone who can push to a
release branch can also change its gates. Protect release branches and tags,
and require review for `.siliconflow/`.

## When it runs

`services/outcome.py` queues an `evaluate_gates` job when a build moves to
`passed`. The artifact ingestion job queues another one if the build had
already passed by the time ingestion finished. Whichever job runs once both
are true does the evaluation. Results are written once per build, and later
jobs do nothing.

If GitHub cannot be reached, the job retries with backoff. Every evaluation
writes a `gates.evaluated` row to `audit_log`, with the eligibility and the
failed rule ids.
