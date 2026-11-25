import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { ArchitectureDiagram, Figure, LifecycleDiagram, SequenceDiagram } from "../how/diagrams";
import { SCREENS } from "../how/screens";
import "../how/how.css";

/**
 * A plain-English explanation of SiliconFlow, for someone who has never seen
 * it. Static: it calls no API and shows no build data. The screenshots are
 * captured from the e2e fixtures, which the backend generated from real CI
 * runs (e2e/fixtures/README.md).
 */
export function HowItWorks() {
  return (
    <article className="hw">
      <Hero />
      <Toc />

      <Section id="journey" kicker="01" title="The journey of a commit"
        lead="Every change to the firmware goes through the same six steps. GitHub Actions does the work; SiliconFlow checks it and decides.">
        <Pipeline />
      </Section>

      <Section id="architecture" kicker="02" title="Architecture: Actions executes, SiliconFlow gates"
        lead="The backend never runs firmware code. It receives three kinds of input from GitHub, checks each one, and keeps the result.">
        <Figure label="architecture" minWidth={860}
          caption="Red arrows are the three inputs. Each one is checked before it can change anything.">
          <ArchitectureDiagram />
        </Figure>
      </Section>

      <Section id="trust" kicker="03" title="Verify, don't trust"
        lead="A message from CI is not believed just because it arrived. Each input type has its own proof, and anything that fails it is rejected before it is stored.">
        <TrustModel />
      </Section>

      <Section id="sequence" kicker="04" title="How a run report reaches a verdict"
        lead="A runner reports facts, such as stages, exit codes, coverage, sizes and hashes, and never a verdict. Here is what happens to one report, in order.">
        <Figure label="sequence" minWidth={820}>
          <SequenceDiagram />
        </Figure>
      </Section>

      <Section id="lifecycle" kicker="05" title="Build status lifecycle"
        lead="Status changes in exactly one place in the code: transitions.py. Once a build reaches an end state, it stays there.">
        <Figure label="lifecycle" minWidth={760}
          caption={<>The colours match the status badges used everywhere in this app. <code>errored</code> is kept separate so that a broken runner never counts against the firmware.</>}>
          <LifecycleDiagram />
        </Figure>
      </Section>

      <Section id="gates" kicker="06" title="Passed is not the same as releasable"
        lead="A green build answers one question. Release engineering needs the answer to a second one.">
        <TwoQuestions />
        <Gates />
      </Section>

      <Section id="triage" kicker="07" title="When a build fails, who fixes it?"
        lead="The failed stage's log is classified by rules tested against real captured logs, so the right team gets the problem.">
        <Triage />
      </Section>

      <Section id="screens" kicker="08" title="See it in the app"
        lead="Two real builds, recorded from CI and replayed through the real backend by its test suite, as this app shows them.">
        <Screens />
      </Section>

      <Section id="terms" kicker="09" title="Key terms">
        <Glossary />
      </Section>

      <aside className="hw-cta">
        <div>
          <h2>Ready to look around?</h2>
          <p>
            Open the build list, or run <code>make demo</code> and click <b>Start tour</b> for a
            ten-step walkthrough on replayed sample inputs.
          </p>
        </div>
        <div className="hw-cta__actions">
          <Link className="hw-btn hw-btn--primary" to="/builds">Browse builds</Link>
          <Link className="hw-btn" to="/analytics">Open analytics</Link>
        </div>
      </aside>
    </article>
  );
}

/* ------------------------------------------------------------ building blocks */

function Section(props: { id: string; kicker: string; title: string; lead?: string; children: ReactNode }) {
  return (
    <section className="hw-section" id={props.id} aria-labelledby={`${props.id}-title`}>
      <header className="hw-section__head">
        <span className="hw-kicker">{props.kicker}</span>
        <h2 id={`${props.id}-title`}>{props.title}</h2>
        {props.lead && <p className="hw-lead">{props.lead}</p>}
      </header>
      {props.children}
    </section>
  );
}

function Hero() {
  return (
    <header className="hw-hero">
      <div className="hw-hero__text">
        <span className="hw-eyebrow">How it works</span>
        <h1>
          From a firmware commit to a binary you can <em>prove</em> is safe to ship.
        </h1>
        <p>
          Companies like Silicon Labs write the firmware that runs inside chips, and a mistake can reach
          millions of devices. Every time an engineer changes it, CI builds and tests it. SiliconFlow
          watches every one of those builds. It checks that the file being released is exactly the one
          that was built and tested, that it meets the team's rules for size and quality, and, when
          something breaks, who should fix it.
        </p>
      </div>
      <div className="hw-hero__card">
        <p className="hw-quote">
          CI tells you the job went green. <b>SiliconFlow tells you whether this exact binary is allowed
          to ship</b>, and if not, exactly why.
        </p>
        <ul className="hw-pillars">
          <li><Icon name="play" /> <span><b>Actions executes.</b> Firmware is compiled and tested in GitHub Actions.</span></li>
          <li><Icon name="shield" /> <span><b>Nothing is trusted.</b> Reports, webhooks and files are all verified.</span></li>
          <li><Icon name="gavel" /> <span><b>The backend decides.</b> Status and release eligibility are decided in one place.</span></li>
        </ul>
      </div>
    </header>
  );
}

const TOC: [string, string][] = [
  ["journey", "Journey"],
  ["architecture", "Architecture"],
  ["trust", "Trust model"],
  ["sequence", "Run report"],
  ["lifecycle", "Lifecycle"],
  ["gates", "Release gates"],
  ["triage", "Failure triage"],
  ["screens", "Screens"],
  ["terms", "Key terms"],
];

function Toc() {
  return (
    <nav className="hw-toc" aria-label="on this page">
      {TOC.map(([id, label]) => (
        <a key={id} href={`#${id}`}>{label}</a>
      ))}
    </nav>
  );
}

/* ------------------------------------------------------------ 01 pipeline */

const STEPS: { lane: "actions" | "sf"; title: string; body: string; icon: IconName }[] = [
  { lane: "actions", title: "Push or PR", body: "A commit to main, a pull request, a release tag or a manual build.", icon: "commit" },
  { lane: "actions", title: "Build & test", body: "Compile for each target, run Unity tests, cppcheck, coverage and size.", icon: "chip" },
  { lane: "actions", title: "Report facts", body: "Each job posts a signed run report. It has no pass or fail field.", icon: "doc" },
  { lane: "sf", title: "Verify", body: "Check the OIDC token, the webhook signature and every file's SHA-256.", icon: "shield" },
  { lane: "sf", title: "Decide status", body: "transitions.py moves the build to passed, failed or errored.", icon: "gavel" },
  { lane: "sf", title: "Gate the release", body: "gates.yml at that commit decides if the binary may ship.", icon: "flag" },
];

function Pipeline() {
  return (
    <div className="hw-pipe">
      <div className="hw-pipe__lanes" aria-hidden>
        <span className="hw-pipe__lane">GitHub Actions · executes</span>
        <span className="hw-pipe__lane hw-pipe__lane--sf">SiliconFlow · records, verifies, gates</span>
      </div>
      <ol className="hw-pipe__steps">
        {STEPS.map((s, i) => (
          <li key={s.title} className={`hw-step hw-step--${s.lane}`}>
            <span className="hw-step__n">{i + 1}</span>
            <Icon name={s.icon} />
            <h3>{s.title}</h3>
            <p>{s.body}</p>
          </li>
        ))}
      </ol>
    </div>
  );
}

/* ------------------------------------------------------------ 03 trust */

function TrustModel() {
  const cards: { icon: IconName; input: string; from: string; check: ReactNode; reject: string }[] = [
    {
      icon: "doc",
      input: "Run report",
      from: "Posted by each Actions job when it ends (sfci.run-report/1).",
      check: <>A <b>GitHub OIDC JWT</b>. Its repository, run, attempt and commit SHA claims must match the build.</>,
      reject: "Rejected if the token or a claim does not match. A report that includes a status is rejected with 422.",
    },
    {
      icon: "bolt",
      input: "Webhook",
      from: "workflow_run and workflow_job events from the GitHub App.",
      check: <>An <b>HMAC-SHA256</b> signature, then deduplicated on <code>X-GitHub-Delivery</code>. Every handler is idempotent.</>,
      reject: "Rejected if unsigned or forged. A redelivered event changes nothing.",
    },
    {
      icon: "box",
      input: "Artifact",
      from: "The ELF and BIN files, downloaded by the worker from Actions.",
      check: <>Every file is <b>re-hashed with SHA-256</b> on the server and checked against the report's manifest.</>,
      reject: "If any file disagrees, nothing is stored. Files that match are stored by their hash.",
    },
  ];
  return (
    <div className="hw-cards hw-cards--3">
      {cards.map((c) => (
        <div key={c.input} className="hw-card">
          <div className="hw-card__head">
            <span className="hw-card__icon"><Icon name={c.icon} /></span>
            <h3>{c.input}</h3>
          </div>
          <p className="muted">{c.from}</p>
          <div className="hw-card__check">
            <span className="hw-tag hw-tag--brand">Checked by</span>
            <p>{c.check}</p>
          </div>
          <div className="hw-card__check">
            <span className="hw-tag">If it fails</span>
            <p>{c.reject}</p>
          </div>
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------ 06 gates */

function TwoQuestions() {
  return (
    <div className="hw-cards hw-cards--2 hw-questions">
      <div className="hw-card">
        <span className="hw-tag">Question 1</span>
        <h3>Did the build pass?</h3>
        <p>Answered by <code>transitions.py</code> from the stages the runner reported and GitHub's own workflow result.</p>
        <p className="hw-outcome">
          <span className="status status--passed"><span className="status__dot" />passed</span>
          <span className="status status--failed"><span className="status__dot" />failed</span>
          <span className="status status--neutral"><span className="status__dot" />errored</span>
        </p>
      </div>
      <div className="hw-card hw-card--brand">
        <span className="hw-tag hw-tag--brand">Question 2</span>
        <h3>May this exact binary ship?</h3>
        <p>
          Answered only after the build passed, by <code>.siliconflow/gates.yml</code> <em>as it was at that
          build's commit</em>. Pull request builds are never eligible.
        </p>
        <p className="hw-outcome">
          <span className="hw-verdict hw-verdict--yes">release-eligible</span>
          <span className="hw-verdict hw-verdict--no">not eligible · names the rule</span>
        </p>
      </div>
    </div>
  );
}

const GATES: [string, string][] = [
  ["required_stages", "configure, compile, test, analyze and package all passed"],
  ["test_failures", "failed + errored Unity cases, from server-verified JUnit, are within the limit"],
  ["line_coverage_min", "gcovr line coverage is at or above the minimum"],
  ["analysis_errors_max", "cppcheck error-severity findings are within the limit"],
  ["text_bytes_max", ".text size per profile, read by the server from the stored ELF"],
  ["release_refs", "the build ran on a release ref: main or a v* tag"],
];

function Gates() {
  return (
    <div className="hw-gates">
      <div className="hw-gates__in">
        <span className="status status--passed"><span className="status__dot" />passed</span>
        <span className="muted">build</span>
      </div>
      <svg className="hw-gates__arrow" viewBox="0 0 40 16" aria-hidden><path d="M0 8H34M28 2l7 6-7 6" /></svg>
      <ol className="hw-gates__list">
        {GATES.map(([rule, text]) => (
          <li key={rule}>
            <code>{rule}</code>
            <span>{text}</span>
          </li>
        ))}
      </ol>
      <svg className="hw-gates__arrow" viewBox="0 0 40 16" aria-hidden><path d="M0 8H34M28 2l7 6-7 6" /></svg>
      <div className="hw-gates__out">
        <div className="hw-verdict hw-verdict--yes">All pass → <b>release-eligible</b></div>
        <div className="hw-verdict hw-verdict--no">Any fail or missing measurement → <b>not eligible</b></div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------ 07 triage */

function Triage() {
  const owners: { who: string; icon: IconName; lead: string; classes: [string, string][] }[] = [
    {
      who: "Firmware developer",
      icon: "code",
      lead: "The code needs a fix. The failure card links the first error to its file, line and log line.",
      classes: [
        ["compile_error", "error: from GCC, -Werror, CMake"],
        ["link_error", "undefined reference"],
        ["size_overflow", "region 'FLASH' overflowed"],
        ["test_failure", "Unity FAIL, sanitizer error"],
        ["analysis", "cppcheck error finding"],
      ],
    },
    {
      who: "Hardware lab",
      icon: "chip",
      lead: "A debug probe, serial port or board in the lab needs attention, not the firmware.",
      classes: [["infra_hw", "no J-Link or CMSIS-DAP probe, OpenOCD init failed"]],
    },
    {
      who: "CI platform team",
      icon: "server",
      lead: "The runner, its disk or its network broke, not the firmware.",
      classes: [["infra_ci", "disk full, compiler OOM-killed, DNS failure"]],
    },
  ];
  return (
    <div className="hw-triage">
      <div className="hw-triage__src">
        <div className="hw-chipbox"><span className="status status--failed"><span className="status__dot" />failed stage log</span></div>
        <svg className="hw-triage__arrow" viewBox="0 0 40 16" aria-hidden><path d="M0 8H34M28 2l7 6-7 6" /></svg>
        <div className="hw-chipbox hw-chipbox--brand"><code>classify.py</code><span className="muted">first error line + class</span></div>
      </div>
      <svg className="hw-fan" viewBox="0 0 300 36" preserveAspectRatio="none" aria-hidden>
        <path d="M150 0V14M50 14H250M50 14V36M150 14V36M250 14V36" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="hw-cards hw-cards--3">
        {owners.map((o) => (
          <div key={o.who} className="hw-card">
            <div className="hw-card__head">
              <span className="hw-card__icon"><Icon name={o.icon} /></span>
              <h3>{o.who}</h3>
            </div>
            <p className="hw-card__lead">{o.lead}</p>
            <ul className="hw-classes">
              {o.classes.map(([cls, cause]) => (
                <li key={cls}><code>{cls}</code><span className="muted">{cause}</span></li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------ 08 screens */

function Screens() {
  return (
    <div className="hw-screens">
      {SCREENS.map((s) => (
        <figure key={s.src} className="hw-shot">
          <div className="hw-shot__frame">
            <span className="hw-shot__dots" aria-hidden><i /><i /><i /></span>
            <img src={s.src} alt={s.alt} width={s.width} height={s.height} loading="lazy" />
          </div>
          <figcaption><b>{s.title}</b> {s.caption}</figcaption>
        </figure>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------ 09 glossary */

const TERMS: [string, string][] = [
  ["Run report", "The JSON facts a CI job posts at the end (sfci.run-report/1). Facts only, never a verdict."],
  ["Profile", "One build flavour: host-cmake, cortex-m4 or siwx917-soc."],
  ["Gate", "One rule in gates.yml, such as line_coverage_min."],
  ["Release-eligible", "The build passed, and every gate passed, on a release ref."],
  ["errored", "The CI infrastructure broke, so the firmware is not blamed."],
  ["HIL", "Hardware-in-the-loop: flashing a real BRD4338A (SiWx917) board and checking it boots."],
  ["Content-addressed", "Files are stored under their own SHA-256, so the same bytes always have the same name."],
  ["OIDC JWT", "A short-lived token GitHub signs for each job, proving which repo, run and commit sent the report."],
];

function Glossary() {
  return (
    <dl className="hw-terms">
      {TERMS.map(([t, d]) => (
        <div key={t}>
          <dt>{t}</dt>
          <dd>{d}</dd>
        </div>
      ))}
    </dl>
  );
}

/* ------------------------------------------------------------ icons */

type IconName = "commit" | "chip" | "doc" | "shield" | "gavel" | "flag" | "play" | "bolt" | "box" | "code" | "server";

const ICONS: Record<IconName, ReactNode> = {
  commit: <><circle cx="12" cy="12" r="3.5" /><path d="M3 12h5.5M15.5 12H21" /></>,
  chip: <><rect x="6.5" y="6.5" width="11" height="11" rx="1.5" /><path d="M9.5 3v3.5M14.5 3v3.5M9.5 17.5V21M14.5 17.5V21M3 9.5h3.5M3 14.5h3.5M17.5 9.5H21M17.5 14.5H21" /></>,
  doc: <><path d="M14 3H7a1.5 1.5 0 0 0-1.5 1.5v15A1.5 1.5 0 0 0 7 21h10a1.5 1.5 0 0 0 1.5-1.5V7.5z" /><path d="M14 3v4.5h4.5M9 12.5h6M9 16h4" /></>,
  shield: <><path d="M12 3l7.5 3v5.5c0 4.6-3.2 8.2-7.5 9.5-4.3-1.3-7.5-4.9-7.5-9.5V6z" /><path d="M8.8 12.2l2.2 2.2 4.3-4.6" /></>,
  gavel: <><path d="M13.5 4.5l6 6M10.5 7.5l6 6M12 6l-4.5 4.5 3 3L15 9" /><path d="M9 12l-5.5 5.5 3 3L12 15M14 20.5h7" /></>,
  flag: <><path d="M5 21V4M5 4.5h11l-2.2 3.8L16 12H5" /></>,
  play: <><circle cx="12" cy="12" r="9" /><path d="M10 8.5v7l5.5-3.5z" /></>,
  bolt: <path d="M13 2.5L5 13.5h6l-1 8 8-11h-6z" />,
  box: <><path d="M12 2.8l8 4.4v9.6l-8 4.4-8-4.4V7.2z" /><path d="M4 7.2l8 4.4 8-4.4M12 11.6v9.6" /></>,
  code: <path d="M8.5 7L3.5 12l5 5M15.5 7l5 5-5 5M13.5 4.5l-3 15" />,
  server: <><rect x="3.5" y="4" width="17" height="7" rx="1.5" /><rect x="3.5" y="13" width="17" height="7" rx="1.5" /><path d="M7 7.5h.01M7 16.5h.01" /></>,
};

function Icon({ name }: { name: IconName }) {
  return (
    <svg className="hw-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      {ICONS[name]}
    </svg>
  );
}
