import type { BuildDetail, TestOutcome } from "../../api/client";
import { useTests } from "../../api/queries";
import { EmptyState, ErrorState, Loading } from "../States";

export const TEST_OUTCOMES = ["failed", "error", "skipped", "passed"] as const satisfies readonly TestOutcome[];
const TONE: Record<TestOutcome, string> = { failed: "failed", error: "failed", skipped: "neutral", passed: "passed" };

export function TestsTab(props: {
  build: BuildDetail;
  outcomes: TestOutcome[];
  setOutcomes: (outcomes: TestOutcome[]) => void;
}) {
  const tests = useTests(props.build);
  if (tests.isPending) return <Loading />;
  if (tests.isError) return <ErrorState error={tests.error} onRetry={tests.refetch} />;

  const all = tests.data;
  if (all.length === 0) {
    return (
      <EmptyState title="No test results recorded">
        {props.build.artifacts_state === "pending"
          ? "JUnit files arrive with the run's artifact, which is still being ingested."
          : "Test cases come from the JUnit files the run uploaded; this build has none."}
      </EmptyState>
    );
  }
  const counts = Object.fromEntries(TEST_OUTCOMES.map((o) => [o, all.filter((t) => t.outcome === o).length]));
  const shown = props.outcomes.length ? all.filter((t) => props.outcomes.includes(t.outcome)) : all;
  const toggle = (o: TestOutcome) =>
    props.setOutcomes(props.outcomes.includes(o) ? props.outcomes.filter((x) => x !== o) : [...props.outcomes, o]);

  return (
    <>
      <div className="tabbar-tools">
        <div className="chips" role="group" aria-label="outcome">
          {TEST_OUTCOMES.map((o) => (
            <button
              key={o}
              type="button"
              className={`chip chip--${TONE[o]}`}
              aria-pressed={props.outcomes.includes(o)}
              onClick={() => toggle(o)}
            >
              {o} <span className="muted">{counts[o]}</span>
            </button>
          ))}
        </div>
        <span className="muted mono">
          {shown.length} of {all.length}
        </span>
      </div>
      <table className="table table--fixed" aria-label="test cases">
        <colgroup>
          <col style={{ width: 90 }} />
          <col style={{ width: "22%" }} />
          <col style={{ width: "28%" }} />
          <col style={{ width: 90 }} />
          <col />
        </colgroup>
        <thead>
          <tr>
            <th>Outcome</th>
            <th>Suite</th>
            <th>Test</th>
            <th className="num">Time</th>
            <th>Message</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((t) => (
            <tr key={`${t.suite}/${t.name}`}>
              <td className={`mono tone--${TONE[t.outcome]}`}>{t.outcome}</td>
              <td className="mono truncate" title={t.suite}>{t.suite}</td>
              <td className="mono truncate" title={t.classname ?? t.name}>{t.name}</td>
              <td className="num mono">{t.duration_ms != null ? `${t.duration_ms} ms` : "—"}</td>
              <td className="mono wrap">{t.message}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
