import type { ReactNode } from "react";

/** Build ids by scenario, then by the scenario's build key (demo/scenarios/*.yml). */
export type BuildIds = Record<string, Record<string, string>>;

export interface TourStep {
  id: string;
  title: string;
  /** For anyone, engineer or not. */
  plain: ReactNode;
  /** Shown under "Technical detail". */
  technical: ReactNode;
  /** Replayed when the step opens, unless already replayed. */
  scenario?: { name: string; speed: number; after?: string };
  /** Where to go; null while the build it needs does not exist yet. */
  route?: (ids: BuildIds) => string | null;
  /** CSS selector of the element to highlight. */
  target?: string;
  /** Show the replay's requests and their answers for this scenario's step. */
  events?: { scenario: string; step: string };
}

const build = (scenario: string, key: string, query = "") => (ids: BuildIds) =>
  ids[scenario]?.[key] ? `/builds/${ids[scenario][key]}${query}` : null;

export const TOUR: TourStep[] = [
  {
    id: "problem",
    title: "The problem, in one sentence",
    plain: (
      <>
        <p>
          Most build systems tell you the job went green. SiliconFlow tells you whether <em>this exact firmware
          file</em> is allowed to ship to devices, and if not, exactly why.
        </p>
        <p>
          Firmware ends up on chips that are hard to recall, so “it built” is not enough. In the background, five weeks
          of sample history is being replayed now so the trends page has data at the end of the tour.
        </p>
      </>
    ),
    technical: (
      <p>
        GitHub Actions executes the builds. SiliconFlow records, correlates and gates: runners send facts in a signed
        run report (sfci.run-report/1), and only the backend decides build status and release eligibility. In demo mode
        those inputs are recorded samples, replayed through the same API endpoints and worker.
      </p>
    ),
    scenario: { name: "history", speed: 20 },
    route: () => "/",
  },
  {
    id: "clean",
    title: "A clean build",
    plain: (
      <p>
        A developer pushes a change. Watch the new build at the top of the list move from <b>queued</b> to{" "}
        <b>running</b> to <b>passed</b>. Nobody refreshes the page: GitHub tells SiliconFlow as each step happens.
      </p>
    ),
    technical: (
      <p>
        Status changes come from workflow_run and workflow_job webhooks, each verified with HMAC-SHA256 and
        deduplicated on X-GitHub-Delivery. The status moves only through transitions.py; passed or failed is decided
        from the run report’s stages once the run completes.
      </p>
    ),
    scenario: { name: "clean_main", speed: 1, after: "history" },
    route: () => "/builds",
    target: '[data-tour="builds-table"]',
  },
  {
    id: "details",
    title: "What happened inside the build",
    plain: (
      <p>
        Each bar is one step of the build: set up, compile, run the tests, check the code, package the firmware. Every
        test result here was read by the server from the test report itself, not taken on the build machine’s word.
      </p>
    ),
    technical: (
      <p>
        Stages come from the OIDC-authenticated run report. The JUnit file is part of the Actions artifact; the worker
        downloads it, checks its size and SHA-256 against the report, then parses each Unity test case.
      </p>
    ),
    route: build("clean_main", "clean"),
    target: '[data-tour="stage-timeline"]',
  },
  {
    id: "artifacts",
    title: "The firmware file, and the rules for shipping it",
    plain: (
      <p>
        These are the actual firmware files. The long code next to each is its <b>fingerprint</b>: the server
        calculated it itself. Below are the <b>release rules</b> (tests, coverage, code checks, size, branch). Every rule
        passed, so this file is OK to ship.
      </p>
    ),
    technical: (
      <p>
        The worker re-hashes every file with SHA-256, checks it against the run report’s manifest and stores it
        content-addressed. Gates come from .siliconflow/gates.yml at the build’s commit; text_bytes_max is measured by
        the server from the stored ELF. Every gate must pass for release_eligible.
      </p>
    ),
    route: build("clean_main", "clean", "?tab=artifacts"),
    target: '[data-tour="artifacts"]',
  },
  {
    id: "compile_error",
    title: "When the code does not compile",
    plain: (
      <p>
        Someone pushed a typo. The build fails, and instead of making an engineer dig through thousands of log lines,
        SiliconFlow points at the exact file and line, and says what kind of problem it is: a <b>compile error</b>,
        which the firmware developer fixes.
      </p>
    ),
    technical: (
      <p>
        The compile stage log (captured gcc output: ‘SL_MAVG_LIMIT’ undeclared) is redacted, stored gzipped and run
        through classify.py, which finds the first error with its file, line and column and classifies the build
        compile_error.
      </p>
    ),
    scenario: { name: "compile_error", speed: 1 },
    route: build("compile_error", "broken"),
    target: '[data-tour="failure-card"]',
  },
  {
    id: "not_releasable",
    title: "Passed, but not allowed to ship",
    plain: (
      <p>
        This build passed every test, but the firmware is too big for the size limit the team set. A normal build
        system would show green. SiliconFlow shows green <em>and</em> blocks the release, naming the one rule that
        failed.
      </p>
    ),
    technical: (
      <p>
        This commit’s gates.yml lowers the host-cmake text_bytes_max to 20480 bytes. The server reads the .text size
        from the stored ELF (30063 bytes); the run report states no size at all. One failed gate makes every artifact
        of the build not release-eligible.
      </p>
    ),
    scenario: { name: "passed_not_releasable", speed: 1 },
    route: build("passed_not_releasable", "too_big", "?tab=artifacts"),
    target: '[data-tour="gates"]',
  },
  {
    id: "redelivery",
    title: "The same message, three times",
    plain: (
      <p>
        Networks retry. Here GitHub sends the “build finished” message three times. SiliconFlow recognises the copies,
        so there is still exactly one build, and its result does not change. Below are the real answers the server gave.
      </p>
    ),
    technical: (
      <p>
        The workflow_run completed delivery is sent three times with one X-GitHub-Delivery id. The first is stored and
        queued (202 accepted); the others hit the unique key and are answered 200 duplicate. Every handler is
        idempotent.
      </p>
    ),
    scenario: { name: "redelivery", speed: 1 },
    route: build("redelivery", "redelivered"),
    events: { scenario: "redelivery", step: "workflow_run.completed" },
  },
  {
    id: "tamper",
    title: "A file that was changed is rejected",
    plain: (
      <p>
        Here the firmware file was altered after the build said what it should be. The server’s fingerprint does not
        match, so nothing is stored and the release is blocked. You cannot ship a file nobody can vouch for.
      </p>
    ),
    technical: (
      <p>
        One byte of firmware.bin was flipped in the Actions artifact after the manifest was written. The re-hash
        disagrees with the manifest, so the whole set is rejected, nothing is stored, an audit_log row records why, and
        the artifacts_verified gate fails. The build itself still passed.
      </p>
    ),
    scenario: { name: "tamper", speed: 1 },
    route: build("tamper", "tampered", "?tab=artifacts"),
    target: '[data-tour="artifacts"]',
  },
  {
    id: "who_fixes",
    title: "Who needs to fix it",
    plain: (
      <>
        <p>
          Not every red mark is a software bug. Here, loading the firmware onto a real Silicon Labs board failed
          because the lab machine had no debug probe plugged in. SiliconFlow labels that <b>infra_hw</b>, a job for the hardware
          lab, so the developer is not blamed for it.
        </p>
        <ul className="tour__list">
          <li><b>compile, link, size, test, analysis errors</b>: the firmware developer</li>
          <li><b>infra_hw</b> (no probe, board not answering): the hardware lab</li>
          <li><b>infra_ci</b> (disk full, out of memory, network): the CI platform team</li>
        </ul>
      </>
    ),
    technical: (
      <p>
        hil.yml’s linked report names the siwx917-soc build it tested. Its flash step exited 75 (ci/hil/flash.sh: probe
        not found), so the hil stage fails and classify.exit_code_index() marks it infra_hw. The build stays passed:
        hardware results never change a finished build.
      </p>
    ),
    scenario: { name: "infra_hw", speed: 1 },
    route: build("infra_hw", "soc", "?stage=hil"),
    target: '[data-tour="stage-timeline"]',
  },
  {
    id: "analytics",
    title: "Trends over time",
    plain: (
      <p>
        Over weeks, the team can see how often builds pass, how long they take, whether the firmware is growing towards
        its flash limit, and which tests are flaky (pass and fail on the same code). These charts come from the sample
        history replayed at the start.
      </p>
    ),
    technical: (
      <p>
        Every figure is SQL over recorded builds; the page says “not enough data” below 10 builds and never pads a
        chart. Builds are grouped by when the server recorded them, so the replayed history lands in this week.
      </p>
    ),
    route: () => "/analytics",
    target: '[data-tour="analytics"]',
  },
];
