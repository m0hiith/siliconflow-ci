/**
 * Diagrams for the "How it works" page. Inline SVG coloured only with theme
 * tokens (how.css), so they follow light and dark. Each mirrors a diagram in
 * the README; the facts come from the code they describe.
 */
import type { ReactNode } from "react";
import type { BuildStatus } from "../api/client";
import { statusTone } from "../status";

function Arrowheads() {
  return (
    <defs>
      <marker id="hw-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M0 0L10 5L0 10z" className="d-head" />
      </marker>
      <marker id="hw-arrow-brand" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M0 0L10 5L0 10z" className="d-head--brand" />
      </marker>
    </defs>
  );
}

function Box(props: {
  x: number; y: number; w: number; h: number;
  title: string; lines?: string[]; tone?: "brand" | "plain"; mono?: boolean;
}) {
  const { x, y, w, h, title, lines = [], tone = "plain", mono } = props;
  return (
    <g>
      <rect x={x} y={y} width={w} height={h} rx={10} className={`d-box${tone === "brand" ? " d-box--brand" : ""}`} />
      <text x={x + 14} y={y + 24} className={mono ? "d-title d-title--mono" : "d-title"}>{title}</text>
      {lines.map((l, i) => (
        <text key={i} x={x + 14} y={y + 44 + i * 17} className="d-sub">{l}</text>
      ))}
    </g>
  );
}

function Cylinder(props: { x: number; y: number; w: number; h: number; title: string; sub: string }) {
  const { x, y, w, h, title, sub } = props;
  const ry = 8;
  return (
    <g>
      <path
        className="d-box"
        d={`M${x} ${y + ry} a${w / 2} ${ry} 0 0 1 ${w} 0 v${h - 2 * ry} a${w / 2} ${ry} 0 0 1 ${-w} 0 z`}
      />
      <path className="d-line" d={`M${x} ${y + ry} a${w / 2} ${ry} 0 0 0 ${w} 0`} fill="none" />
      <text x={x + w / 2} y={y + h / 2 + 6} textAnchor="middle" className="d-title">{title}</text>
      <text x={x + w / 2} y={y + h / 2 + 23} textAnchor="middle" className="d-sub">{sub}</text>
    </g>
  );
}

/** A labelled horizontal or bent connector. `label` sits above the first segment. */
function Wire(props: { d: string; label?: string; sub?: string; lx?: number; ly?: number; brand?: boolean }) {
  return (
    <g>
      <path d={props.d} className={props.brand ? "d-wire d-wire--brand" : "d-wire"} markerEnd={`url(#${props.brand ? "hw-arrow-brand" : "hw-arrow"})`} />
      {props.label && (
        <text x={props.lx} y={props.ly} textAnchor="middle" className="d-label">{props.label}</text>
      )}
      {props.sub && (
        <text x={props.lx} y={(props.ly ?? 0) + 30} textAnchor="middle" className="d-sub d-sub--mono">{props.sub}</text>
      )}
    </g>
  );
}

export function Figure(props: { label: string; minWidth: number; children: ReactNode; caption?: ReactNode }) {
  return (
    <figure className="hw-figure">
      <div className="hw-figure__scroll">
        <div style={{ minWidth: props.minWidth }}>{props.children}</div>
      </div>
      {props.caption && <figcaption>{props.caption}</figcaption>}
    </figure>
  );
}

/* ------------------------------------------------------------ architecture */

export function ArchitectureDiagram() {
  const jobs: [string, string][] = [
    ["host-cmake", "configure → compile → test → analyze → package → coverage"],
    ["cortex-m4", "configure → compile → package → size"],
    ["siwx917-soc", "generate → compile → package"],
    ["hil · BRD4338A", "fetch → flash → boot check (self-hosted)"],
  ];
  return (
    <svg viewBox="0 0 1080 580" className="hw-svg" role="img"
      aria-label="Architecture: an engineer pushes to GitHub; GitHub Actions runs the firmware jobs; SiliconFlow receives signed webhooks and run reports, downloads and re-hashes artifacts, stores everything in Postgres and a content-addressed store, and serves the UI.">
      <Arrowheads />
      {/* Left: GitHub side */}
      <rect x={24} y={40} width={150} height={64} rx={32} className="d-pill" />
      <text x={99} y={68} textAnchor="middle" className="d-title">Engineer</text>
      <text x={99} y={86} textAnchor="middle" className="d-sub">push · PR · tag</text>
      <Wire d="M174 72 H210" />
      <Box x={214} y={32} w={250} h={80} title="GitHub" lines={["repository + GitHub App", "sends workflow events"]} />
      <Wire d="M339 112 V160" />
      <text x={350} y={142} className="d-sub">starts workflows</text>

      <rect x={24} y={160} width={440} height={400} rx={14} className="d-group" />
      <text x={44} y={188} className="d-lane">GITHUB ACTIONS · EXECUTES</text>
      {jobs.map(([name, stages], i) => (
        <g key={name}>
          <rect x={44} y={202 + i * 64} width={400} height={54} rx={8} className="d-box" />
          <text x={58} y={224 + i * 64} className="d-title d-title--mono">{name}</text>
          <text x={58} y={243 + i * 64} className="d-sub d-sub--sm">{stages}</text>
        </g>
      ))}
      <Cylinder x={44} y={466} w={400} h={74} title="Actions artifacts" sub="ELF + BIN uploaded by each job" />

      {/* Corridor: the three inputs, each with its own check */}
      <Wire d="M464 80 H638" label="webhook events" sub="HMAC-SHA256" lx={551} ly={72} brand />
      <Wire d="M464 230 H638" label="run report" sub="GitHub OIDC JWT" lx={551} ly={222} brand />
      <Wire d="M444 503 H638" label="artifact zip" sub="re-hashed, SHA-256" lx={551} ly={495} brand />

      {/* Right: SiliconFlow */}
      <rect x={640} y={24} width={416} height={536} rx={14} className="d-group d-group--brand" />
      <text x={660} y={50} className="d-lane d-lane--brand">SILICONFLOW · RECORDS, VERIFIES, GATES</text>
      <Box x={660} y={62} w={226} h={186} tone="brand" title="FastAPI backend"
        lines={["verifies webhook HMAC", "verifies OIDC claims", "dedupes X-GitHub-Delivery", "rejects a report with a status", "transitions.py owns status", "never runs user code"]} />
      <Wire d="M773 248 V326" />
      <text x={782} y={292} className="d-sub">queues jobs</text>
      <Box x={660} y={330} w={226} h={190} tone="brand" title="Worker"
        lines={["downloads artifacts (GitHub App)", "re-hashes every file", "stores files by SHA-256", "parses JUnit test results", "classifies failed stage logs", "evaluates gates.yml", "reconciles missed events"]} />

      <Box x={914} y={62} w={122} h={76} title="React UI" lines={["this app"]} />
      <Wire d="M914 100 H890" />
      <Cylinder x={914} y={186} w={122} h={92} title="Postgres" sub="builds · facts" />
      <Wire d="M886 220 H910" />
      <Wire d="M886 380 C900 380 902 300 940 282" />
      <Cylinder x={914} y={428} w={122} h={92} title="Artifact store" sub="sha256/ab/cd/…" />
      <Wire d="M886 474 H910" />
    </svg>
  );
}

/* ------------------------------------------------------------ run report sequence */

type Msg = { from: number; to: number; label: string; reply?: boolean };

export function SequenceDiagram() {
  const lanes = ["Runner (Actions job)", "SiliconFlow API", "Worker", "GitHub"];
  const XS = [170, 410, 650, 870];
  const lx = (lane: number) => XS[lane] ?? 0;
  const msgs: Msg[] = [
    { from: 0, to: 1, label: "POST /api/v1/runs/report + OIDC JWT" },
    { from: 1, to: 1, label: "JWT claims match repo, run, attempt, SHA" },
    { from: 1, to: 0, label: "202 accepted · ingest queued", reply: true },
    { from: 2, to: 3, label: "download Actions artifact" },
    { from: 2, to: 2, label: "re-hash, check manifest, store" },
    { from: 2, to: 2, label: "parse JUnit, classify logs" },
    { from: 3, to: 1, label: "workflow_run completed · HMAC-signed" },
    { from: 1, to: 1, label: "transitions.py settles status" },
    { from: 2, to: 2, label: "evaluate gates.yml at the commit" },
  ];
  const top = 72;
  const row = 46;
  const bottom = top + 30 + msgs.length * row;
  return (
    <svg viewBox={`0 0 980 ${bottom + 20}`} className="hw-svg" role="img"
      aria-label="Sequence: the runner posts a run report with an OIDC JWT; the API checks the claims and accepts it; the worker downloads, re-hashes and stores the artifact, parses JUnit and classifies logs; GitHub's signed webhook arrives; transitions.py settles the status; the worker evaluates the release gates.">
      <Arrowheads />
      {lanes.map((name, i) => (
        <g key={name}>
          <line x1={lx(i)} x2={lx(i)} y1={top} y2={bottom} className="d-lifeline" />
          <rect x={lx(i) - 82} y={20} width={164} height={44} rx={10} className={i === 1 || i === 2 ? "d-box d-box--brand" : "d-box"} />
          <text x={lx(i)} y={47} textAnchor="middle" className="d-title">{name}</text>
        </g>
      ))}
      {msgs.map((m, i) => {
        const y = top + 36 + i * row;
        const badge = (
          <g>
            <circle cx={36} cy={y} r={12} className="d-badge" />
            <text x={36} y={y + 4} textAnchor="middle" className="d-badge__n">{i + 1}</text>
          </g>
        );
        if (m.from === m.to) {
          const x = lx(m.from);
          return (
            <g key={i}>
              {badge}
              <path d={`M${x} ${y - 8} h30 v16 h-28`} className="d-wire" markerEnd="url(#hw-arrow)" />
              <text x={x + 40} y={y + 4} className="d-msg">{m.label}</text>
            </g>
          );
        }
        const x1 = lx(m.from);
        const x2 = lx(m.to);
        const dir = x2 > x1 ? 1 : -1;
        return (
          <g key={i}>
            {badge}
            <line x1={x1} x2={x2 - dir * 3} y1={y} y2={y} className={m.reply ? "d-wire d-wire--dashed" : "d-wire d-wire--brand"}
              markerEnd={m.reply ? "url(#hw-arrow)" : "url(#hw-arrow-brand)"} />
            <text x={(x1 + x2) / 2} y={y - 8} textAnchor="middle" className="d-msg">{m.label}</text>
          </g>
        );
      })}
    </svg>
  );
}

/* ------------------------------------------------------------ status lifecycle */

export function LifecycleDiagram() {
  const terminals: [BuildStatus, string][] = [
    ["cancelled", "someone stopped it"],
    ["passed", "every required stage passed"],
    ["failed", "the firmware broke"],
    ["timed_out", "ran past its time limit"],
    ["errored", "CI infrastructure broke, not the firmware"],
  ];
  const ty = (i: number) => 34 + i * 62;
  return (
    <svg viewBox="0 0 920 384" className="hw-svg" role="img"
      aria-label="Build status lifecycle: queued goes to running or cancelled; running ends as passed, failed, cancelled, timed_out or errored. Terminal states never change.">
      <Arrowheads />
      <g className={`d-state d-state--${statusTone("queued")}`}>
        <rect x={30} y={150} width={130} height={48} rx={24} />
        <text x={95} y={179} textAnchor="middle">queued</text>
      </g>
      <Wire d="M160 174 H244" />
      <g className={`d-state d-state--${statusTone("running")}`}>
        <rect x={250} y={150} width={130} height={48} rx={24} />
        <text x={315} y={179} textAnchor="middle">running</text>
      </g>
      {/* queued → cancelled, over the top */}
      <Wire d="M95 150 V12 H555 V28" />
      {terminals.map(([name, note], i) => (
        <g key={name}>
          <Wire d={`M380 174 C430 174 430 ${ty(i) + 22} 482 ${ty(i) + 22}`} />
          <g className={`d-state d-state--${statusTone(name)}`}>
            <rect x={486} y={ty(i)} width={138} height={44} rx={22} />
            <text x={555} y={ty(i) + 27} textAnchor="middle">{name}</text>
          </g>
          <text x={642} y={ty(i) + 27} className="d-sub">{note}</text>
        </g>
      ))}
      <text x={486} y={ty(4) + 76} className="d-label">Terminal states never change.</text>
    </svg>
  );
}
