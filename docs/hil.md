# Hardware-in-the-loop testing (siwx917-soc)

After a siwx917-soc build passes, `.github/workflows/hil.yml` flashes its
image onto a real BRD4338A on a self-hosted runner. It then checks that the
board boots the commit that was built. The result becomes a `hil` stage on
that build in SiliconFlow.

```
Firmware CI (siwx917-soc) ──completed, success, push to main or v* tag──▶ hil.yml
                                                                     (self-hosted, siwx917)
  fetch  ci/hil/fetch.py       build of that run ─▶ wait until passed and artifacts stored
                               ─▶ download siwx917_soc.rps ─▶ check SHA-256 ─▶ keep it
  flash  ci/hil/flash.sh       serial port free? probe? flash, verify, reset (Commander)
         ci/hil/retry_infra.sh   once more if that exited 75
  test   ci/hil/boot_check.py  open UART ─▶ reset ─▶ exactly one SFCI-BOOT line, right sha
  report ci/report.py --profile siwx917-hil --build-id bld_... ─▶ POST /api/v1/runs/report
                                                                  ─▶ "hil" stage on the build
```

## The boot line

On boot, before anything else, `firmware/siwx917/app.c` prints this line on
the VCOM UART:

```
SFCI-BOOT version=<FW_VERSION> sha=<FW_COMMIT_SHORT>
```

`firmware/siwx917/sfci_boot.h` formats it, and `firmware/tests/test_sfci_boot.c`
tests it on the host. For the slc build, `ci/siwx917.sh generate` writes the
values to `out/gen/autogen/version.h`. They come from `git describe --always
--dirty` and `git rev-parse --short HEAD`, the same values that
`firmware/cmake/gen_version.cmake` writes for CMake builds.

`boot_check.py` opens the port first, then resets the board, so it cannot
miss the line. It passes when exactly one boot line arrives within 15 s and
its sha is a prefix of the build's commit. It then keeps reading for 3 s. A
second boot line means the board reset again, and that fails the test.

## On-target tests (tests/hil)

`tests/hil/` is a pytest suite that goes further than the boot check. It is
run by hand against a board on your desk or on the runner. `hil.yml` does not
run it yet (see "Not done yet").

```
pip install -r tests/hil/requirements.txt
SFCI_SERIAL_PORT=/dev/ttyACM0 SFCI_EXPECTED_SHA=$(git rev-parse HEAD) pytest tests/hil
```

The session opens the UART exclusively, resets the board once with
`SFCI_RESET_CMD` (default `ci/hil/flash.sh reset`), and writes every byte it
receives to `out/logs/hil-serial.log` as the byte arrives. Bytes sent to the
board are not logged, because a command can carry the Wi-Fi passphrase. The
JUnit report goes to `out/junit-hil.xml`, with the firmware's version and sha
as suite properties.

| Test | Passes when | Needs |
| --- | --- | --- |
| `test_boot_banner` | the `SFCI-BOOT` line arrives within 10 s of the reset, exactly once, and its sha is a prefix of the expected commit. It reuses `boot_check.verdict` | `SFCI_EXPECTED_SHA` (40 hex), or `build.json` at `SFCI_HIL_BUILD_JSON` (default `out/hil/build.json`) |
| `test_debounce_on_target` | after `inject-bounce`, the firmware prints exactly one `PRESSED` and then one `RELEASED`, 224 ± 20 ms apart, and does not reset | firmware built with `SFCI_HIL_HOOKS=1` |
| `test_wifi_join` | `wifi-join <ssid> [<psk>]` gets `SFCI-WIFI joined ssid=<ssid> ip=<a.b.c.d>` | `SFCI_TEST_SSID`, optional `SFCI_TEST_PSK`; skipped otherwise |
| `test_ble_advertising` | the host sees a BLE advertisement named `SFCI_TEST_BLE_NAME` (via bleak) | `SFCI_TEST_BLE_NAME` and a Bluetooth adapter; skipped otherwise |

The firmware does not implement Wi-Fi or BLE yet. If you set those variables,
the two radio tests fail and say why. They do not skip, because setting a
variable claims that the firmware under test supports the feature. The
commands and replies in the table are the contract that the firmware has to
meet.

Exit codes follow the table below. pytest's own codes apply, except that a
missing setting exits 2, and a serial port that is missing, busy or fails
mid-run, or a failed reset, exits 75.
`SFCI_SERIAL_PORT` falls back to `SFCI_HIL_SERIAL_PORT`, so the runner's
`.env` works unchanged.

`ci/tests/test_hil_pytest.py` runs the suite against a pseudo-terminal and a
fake board. It skips if pyserial is not installed.

### Test hooks (`SFCI_HIL_HOOKS`)

`SFCI_HIL_HOOKS=1 ci/siwx917.sh generate` writes `#define SFCI_HIL_HOOKS 1`
to `autogen/sfci_build.h`. The default is 0, and a release image must never
set it. With the flag set:

- `app.c` prints `SFCI-HIL hooks enabled` after the boot line, and reads
  commands on the VCOM UART (`firmware/src/sfci_cli.c`). It answers each
  command with one line: `SFCI-CLI ok inject-bounce`, `SFCI-CLI error busy`,
  `SFCI-CLI error unknown-command` or `SFCI-CLI error line-too-long`. Nothing
  is echoed.
- `inject-bounce` starts the script in `firmware/src/sfci_bounce.c`. While
  the script runs, `hal_read_button()` returns the script's level instead of
  BTN0's: bursts of 1–3 ms glitches, a 200 ms hold, and a 15 ms re-contact on
  release. `app.c` statically asserts that each glitch is shorter than its
  20 ms debounce. `firmware/tests/test_sfci_bounce.c` checks the same
  sequence through the real debouncer, at loop periods from 1 to 9 ms and
  across a timer wrap.
- The UART is read without blocking. The SDK's iostream read busy-waits for
  every byte, so instead `hal_siwx917_uart_getc()` keeps one interrupt-driven
  1-byte receive armed on the iostream's USART handle and polls it.

With the flag at 0, none of this is compiled into the image.

## Exit codes

| Code | Meaning | Stage | Retried |
| --- | --- | --- | --- |
| 0 | done | passed | — |
| 1 | the firmware or the build failed: no boot line, wrong sha, rebooted; or fetch failed (build not passed, artifact missing, **SHA-256 mismatch**, SiliconFlow not answering) | failed | never |
| 2 | configuration: missing env var, Commander TODO not filled | failed | never |
| 75 | infrastructure: serial port missing or busy, probe not found, flash, verify or reset failed | failed, `infra_hw` | flash: once |

75 is sysexits' `EX_TEMPFAIL`. Once `fetch.py` has checked the image's SHA-256,
a failure to write it to the board is a hardware problem, not a firmware one.
Tests are never retried.

## Runner setup

- Labels `self-hosted` and `siwx917`, and a GitHub environment named
  `hardware`. Add required reviewers to it if you want a person to approve
  every run.
- `python3` ≥ 3.11 and Simplicity Commander. The workflow needs no pip
  packages, because `boot_check.py` opens the serial port with `termios`.
  Only `tests/hil` needs `tests/hil/requirements.txt`.
- In the runner's `.env` file:

  | Variable | Example | |
  | --- | --- | --- |
  | `SFCI_HIL_DEVICE` | the part, for Commander's `--device` | required |
  | `SFCI_HIL_SERIAL_PORT` | `/dev/ttyACM0` | required |
  | `SFCI_HIL_PROBE_SERIAL` | J-Link serial number | if several probes |
  | `SFCI_HIL_COMMANDER` | `/opt/commander/commander` | default `commander` |
  | `SFCI_HIL_BAUD` | `115200` | default 115200 |

- **Fill in the TODO in `ci/hil/flash.sh`**: four arrays of Commander
  arguments (probe, flash, verify, reset), copied from your working local
  command. Until then, flashing exits 2 and names the empty array.
- The repository variable `SFCI_API_URL` must be set, as for the other
  workflows.

## Trust

- `workflow_run` runs `hil.yml` and `ci/` as they are on the default branch,
  never at the commit under test. The job runs only for successful `push`
  runs from this repository, so pull requests and forks never reach the
  runner.
- The image comes from SiliconFlow's content-addressed store, not from the
  GitHub artifact. `fetch.py` deletes any earlier image first. It moves the
  new image into place only when its SHA-256 and size match the build's
  artifact listing and the `X-Checksum-SHA256` header. It also checks with
  SiliconFlow that the build passed, is `siwx917-soc`, and ran on
  `refs/heads/main` or a `refs/tags/v*` tag.
- `concurrency: hil-siwx917` lets only one run use the board at a time.

## Backend: linked reports

The report is a **linked report**: `profile` and `build_id` name the build it
tested, and it creates no build of its own (`app/services/linked_reports.py`).
`SFCI_GITHUB_HIL_WORKFLOWS` lists the workflows that may post linked reports
(default `{".github/workflows/hil.yml": {"siwx917-hil": "siwx917-soc"}}`).
The backend accepts one only if all of these hold:

- the OIDC token is from a listed workflow, in the build's repository, with
  `event_name` `workflow_run` and `workflow_ref` on the default branch;
- the body describes the token's run, as for any run report;
- the workflow lists the report's profile, and the build has the profile that
  it tests (`siwx917-soc`);
- the build is not a pull request build, and has `passed`.

A `workflow_run` token's `sha` is the default branch's head, not the commit
under test, so the backend cannot match it to the build's commit. The boot
line's sha is what proves the board ran the build's commit.

The report adds a `hil` stage after the build's own stages. That stage is
never required, so the build's status and gates do not change. The stage
passes when `fetch`, `flash` and `test` all passed. Otherwise its exit code
is the first failed stage's, and exit code 75 gives it `failure_class`
`infra_hw`. `GET /api/v1/builds/{id}/stages` shows that class.

Each build has one linked report per profile. An identical retry returns
`200`. A later attempt of the same HIL run replaces the stage, which lets you
re-run after an infrastructure failure, and writes `linked_report.replaced`
to `audit_log`. Anything else gets `409`.

## Not done yet

- `hil.yml` does not run `tests/hil`. It flashes the release image, which has
  no test hooks. Running the suite needs a second siwx917-soc image built
  with `SFCI_HIL_HOOKS=1`, recorded as such in its manifest so that it can
  never be released, and pyserial on the runner.
- The firmware has no Wi-Fi or BLE yet: no `wifi-join` command and no
  advertising.
- `ci/tests/test_hil_pytest.py` skips in the `ci-scripts` job until the
  toolchain image installs pyserial.

- The HIL run's `out/` (stage logs, UART capture) is uploaded to GitHub but
  not ingested, so the `hil` stage has no log in SiliconFlow.
- `hil` is not a release gate. Gates are evaluated once, when the build
  passes, which is before HIL runs.
