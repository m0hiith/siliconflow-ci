import type { ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  byProfile,
  dayOf,
  dayScale,
  failureClassLabel,
  flakyReasons,
  ordinalX,
  weeksBetween,
} from "../analytics";
import type { DurationDay, Durations, FlakyTests, PassRate, SizeCommit, SizeSeries } from "../api/client";
import {
  type AnalyticsScope,
  useDurations,
  useFlakyTests,
  usePassRate,
  useProjects,
  useSizeTrends,
} from "../api/queries";
import { type ChartColumn, PointChart } from "../components/PointChart";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { formatDuration, formatPercent, formatUtc, shortSha } from "../format";
import { statusTone } from "../status";

const WINDOWS = [30, 90, 180] as const;
const MEDIAN = { name: "median", color: "var(--series-1)" };
const P90 = { name: "p90", color: "var(--series-2)" };

function parseScope(params: URLSearchParams): AnalyticsScope {
  const days = Number(params.get("days"));
  return {
    project: params.get("project") || undefined,
    days: (WINDOWS as readonly number[]).includes(days) ? days : WINDOWS[0],
  };
}

export function Analytics() {
  const [params, setParams] = useSearchParams();
  const scope = parseScope(params);
  const projects = useProjects().data ?? [];

  const update = (patch: Partial<AnalyticsScope>) => {
    const next = { ...scope, ...patch };
    const p = new URLSearchParams();
    if (next.project) p.set("project", next.project);
    if (next.days !== WINDOWS[0]) p.set("days", String(next.days));
    setParams(p, { replace: true });
  };

  return (
    <>
      <div className="page-head">
        <h1>Analytics</h1>
        <span className="page-head__meta">computed from recorded builds; days and weeks are UTC</span>
      </div>

      <div className="filters filters--spaced">
        {projects.length > 1 && (
          <label className="field">
            project
            <select value={scope.project ?? ""} onChange={(e) => update({ project: e.target.value || undefined })}>
              <option value="">all</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.slug}
                </option>
              ))}
            </select>
          </label>
        )}
        <span className="field">
          window
          <span className="chips">
            {WINDOWS.map((d) => (
              <button
                key={d}
                type="button"
                className="chip chip--neutral"
                aria-pressed={scope.days === d}
                onClick={() => update({ days: d })}
              >
                {d}d
              </button>
            ))}
          </span>
        </span>
      </div>

      <div className="analytics">
        <DurationsPanel scope={scope} />
        <PassRatePanel scope={scope} />
        <SizesPanel scope={scope} />
        <FlakyPanel scope={scope} />
      </div>
    </>
  );
}

// ------------------------------------------------------------------ shared

function Panel(props: { title: string; meta?: ReactNode; children: ReactNode }) {
  return (
    <section className="panel">
      <div className="panel__head">
        <h2>{props.title}</h2>
        {props.meta && <span className="muted">{props.meta}</span>}
      </div>
      {props.children}
    </section>
  );
}

/** Loading and error states, then the content once the query has data. */
function Query<T>(props: {
  q: { isPending: boolean; isError: boolean; error: Error | null; data: T | undefined; refetch: () => unknown };
  children: (data: T) => ReactNode;
}) {
  const { q } = props;
  if (q.isPending) return <Loading />;
  if (q.isError) return <ErrorState error={q.error!} onRetry={q.refetch} />;
  return <>{props.children(q.data as T)}</>;
}

function NotEnoughData({ builds, min, what }: { builds: number; min: number; what: string }) {
  return (
    <EmptyState title="Not enough data">
      {builds === 0 ? "No" : `Only ${builds}`} {what} {builds === 1 ? "was" : "were"} recorded; this needs at
      least {min}.
    </EmptyState>
  );
}

const bytes = (n: number) => `${n.toLocaleString("en-US")} B`;

// --------------------------------------------------------------- durations

function DurationsPanel({ scope }: { scope: AnalyticsScope }) {
  const q = useDurations(scope);
  return (
    <Panel
      title="Build duration"
      meta="median and p90, start to finish, of passed and failed builds per profile and day"
    >
      <Query q={q}>
        {(d) =>
          !d.enough_data ? (
            <NotEnoughData builds={d.builds} min={d.min_builds} what="passed or failed builds in this window" />
          ) : (
            <div className="multiples">
              {byProfile(d.days).map(([profile, days]) => (
                <DurationChart key={profile} profile={profile} days={days} window={d} />
              ))}
            </div>
          )
        }
      </Query>
    </Panel>
  );
}

function DurationChart({ profile, days, window }: { profile: string; days: DurationDay[]; window: Durations }) {
  const first = dayOf(window.window_start);
  const last = dayOf(window.generated_at);
  const x = dayScale(first, last);
  const columns: ChartColumn[] = days.map((d) => ({
    key: `${profile} ${d.day}`,
    x: x(d.day),
    values: [d.median_seconds, d.p90_seconds],
    tooltip: (
      <>
        <div className="mono">{d.day}</div>
        <div>
          median <b className="mono">{formatDuration(d.median_seconds)}</b> · p90{" "}
          <b className="mono">{formatDuration(d.p90_seconds)}</b>
        </div>
        <div className="muted">
          {d.builds} build{d.builds === 1 ? "" : "s"}
        </div>
      </>
    ),
  }));
  return (
    <div className="multiple">
      <div className="multiple__title mono">{profile}</div>
      <PointChart
        label={`Build duration of ${profile}, median and p90 per day`}
        series={[MEDIAN, P90]}
        columns={columns}
        slots={Math.round((Date.parse(last) - Date.parse(first)) / 86_400_000) + 1}
        xTicks={[
          { x: 0, label: first },
          { x: 1, label: last },
        ]}
        yFormat={formatDuration}
        zeroBased
        range
      />
    </div>
  );
}

// --------------------------------------------------------------- pass rate

function PassRatePanel({ scope }: { scope: AnalyticsScope }) {
  const q = usePassRate(scope);
  return (
    <Panel title="Pass rate per week" meta="passed / (passed + failed); cancelled, timed out and errored builds have no verdict">
      <Query q={q}>
        {(d) =>
          !d.enough_data ? (
            <NotEnoughData builds={d.builds} min={d.min_builds} what="finished builds in this window" />
          ) : (
            <PassRateBody data={d} />
          )
        }
      </Query>
    </Panel>
  );
}

function PassRateBody({ data }: { data: PassRate }) {
  const weeks = weeksBetween(dayOf(data.window_start), dayOf(data.generated_at));
  const byWeek = new Map(data.weeks.map((w) => [w.week_start, w]));
  const columns: ChartColumn[] = data.weeks.map((w) => ({
    key: `week of ${w.week_start}`,
    x: ordinalX(weeks.indexOf(w.week_start), weeks.length),
    values: [w.pass_rate],
    tooltip: (
      <>
        <div className="mono">week of {w.week_start}</div>
        <div>
          pass rate <b className="mono">{w.pass_rate == null ? "—" : formatPercent(w.pass_rate)}</b>
        </div>
        <div className="muted">
          {w.status_counts.passed} passed / {w.status_counts.failed} failed of {w.builds}
        </div>
      </>
    ),
  }));

  return (
    <>
      <div className="multiples">
        <div className="multiple multiple--wide">
          <PointChart
            label="Pass rate per week"
            series={[{ name: "pass rate", color: "var(--series-1)" }]}
            columns={columns}
            slots={weeks.length}
            xTicks={[
              { x: 0, label: weeks[0]! },
              { x: 1, label: weeks[weeks.length - 1]! },
            ]}
            yFormat={(v) => `${Math.round(v * 100)}%`}
            yDomain={[0, 1]}
            bars
          />
        </div>
      </div>
      <div className="table-wrap">
        <table className="table" aria-label="pass rate and failure classes per week">
          <thead>
            <tr>
              <th>Week of</th>
              <th className="num">Builds</th>
              <th className="num">Passed</th>
              <th className="num">Failed</th>
              <th className="num">Timed out</th>
              <th className="num">Errored</th>
              <th className="num">Cancelled</th>
              <th className="num">Pass rate</th>
              <th>Failure classes (failed, timed out, errored)</th>
            </tr>
          </thead>
          <tbody>
            {[...weeks].reverse().map((week) => {
              const w = byWeek.get(week);
              if (!w) {
                return (
                  <tr key={week}>
                    <td className="mono nowrap">{week}</td>
                    <td colSpan={8} className="muted">
                      no finished builds
                    </td>
                  </tr>
                );
              }
              const c = w.status_counts;
              return (
                <tr key={week}>
                  <td className="mono nowrap">{week}</td>
                  <td className="num mono">{w.builds}</td>
                  <td className="num mono tone--passed">{c.passed}</td>
                  <td className="num mono tone--failed">{c.failed}</td>
                  <td className="num mono">{c.timed_out}</td>
                  <td className="num mono">{c.errored}</td>
                  <td className="num mono">{c.cancelled}</td>
                  <td className="num mono">{w.pass_rate == null ? "—" : formatPercent(w.pass_rate)}</td>
                  <td>
                    <span className="breakdown mono">
                      {w.failure_classes.length === 0
                        ? <span className="muted">none</span>
                        : w.failure_classes.map((f) => (
                            <span key={f.failure_class ?? ""}>
                              {f.count} {failureClassLabel(f.failure_class)}
                            </span>
                          ))}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

// ------------------------------------------------------------------- sizes

function SizesPanel({ scope }: { scope: AnalyticsScope }) {
  const q = useSizeTrends(scope);
  return (
    <Panel
      title="Firmware size per commit"
      meta="default branch; flash = text + data, RAM = data + bss, as GNU size reported them; click a point to open its build"
    >
      <Query q={q}>
        {(d) => (
          <div className="size-series">
            {d.series.map((s) => (
              <SizeRow key={s.profile} series={s} min={d.min_builds} />
            ))}
          </div>
        )}
      </Query>
    </Panel>
  );
}

function SizeRow({ series, min }: { series: SizeSeries; min: number }) {
  return (
    <div className="size-series__row">
      <div className="multiple__title mono">
        {series.profile}
        {series.enough_data && (
          <span className="muted">
            {" "}
            · {series.commits.length} commit{series.commits.length === 1 ? "" : "s"}
          </span>
        )}
      </div>
      {!series.enough_data ? (
        <NotEnoughData builds={series.builds} min={min} what={`${series.profile} builds with a size`} />
      ) : (
        <div className="multiples">
          <SizeChart series={series} measure="flash_bytes" title="flash" />
          <SizeChart series={series} measure="ram_bytes" title="RAM" />
        </div>
      )}
    </div>
  );
}

function SizeChart(props: { series: SizeSeries; measure: "flash_bytes" | "ram_bytes"; title: string }) {
  const { commits, profile } = props.series;
  const columns: ChartColumn[] = commits.map((c: SizeCommit, i) => ({
    key: `${shortSha(c.commit_sha)} build ${c.build_number}`,
    x: ordinalX(i, commits.length),
    values: [c[props.measure]],
    href: `/builds/${c.build_id}?tab=size`,
    tooltip: (
      <>
        <div className="mono">
          {shortSha(c.commit_sha)} · #{c.build_number}{" "}
          <span className={`tone--${statusTone(c.status)}`}>{c.status}</span>
        </div>
        <div>
          {props.title} <b className="mono">{bytes(c[props.measure])}</b>
        </div>
        <div className="muted mono">
          text {c.text_bytes} · data {c.data_bytes} · bss {c.bss_bytes}
        </div>
        <div className="muted">{formatUtc(c.created_at)}</div>
      </>
    ),
  }));
  const first = commits[0]!;
  const last = commits[commits.length - 1]!;
  return (
    <div className="multiple">
      <div className="multiple__title">{props.title}</div>
      <PointChart
        label={`${props.title} of ${profile} per commit`}
        series={[{ name: props.title, color: "var(--series-1)" }]}
        columns={columns}
        slots={commits.length}
        xTicks={
          commits.length > 1
            ? [
                { x: 0, label: shortSha(first.commit_sha).slice(0, 7) },
                { x: 1, label: shortSha(last.commit_sha).slice(0, 7) },
              ]
            : [{ x: 0.5, label: shortSha(last.commit_sha).slice(0, 7) }]
        }
        yFormat={bytes}
      />
    </div>
  );
}

// ------------------------------------------------------------- flaky tests

function FlakyPanel({ scope }: { scope: AnalyticsScope }) {
  const q = useFlakyTests(scope);
  return (
    <Panel
      title="Flaky tests"
      meta={
        q.data &&
        `passed and failed on one commit in the last ${scope.days} days, or flipped more than ${q.data.flip_threshold}× over the last ${q.data.recent_builds} default-branch builds of a profile`
      }
    >
      <Query q={q}>
        {(d) =>
          !d.enough_data ? (
            <NotEnoughData builds={d.builds} min={d.min_builds} what="builds with test results" />
          ) : d.tests.length === 0 ? (
            <EmptyState title="No flaky tests">
              None of {d.builds} builds with test results shows a test passing and failing on the same commit, or
              flipping more than {d.flip_threshold}×.
            </EmptyState>
          ) : (
            <FlakyTable data={d} />
          )
        }
      </Query>
    </Panel>
  );
}

function FlakyTable({ data }: { data: FlakyTests }) {
  return (
    <div className="table-wrap">
      <table className="table" aria-label="flaky tests">
        <thead>
          <tr>
            <th>Test</th>
            <th>Profile</th>
            <th>Why</th>
            <th>Example builds</th>
          </tr>
        </thead>
        <tbody>
          {data.tests.map((t) => (
            <tr key={`${t.repository} ${t.profile} ${t.suite} ${t.name}`}>
              <td className="mono">
                {t.suite && <span className="muted">{t.suite} › </span>}
                {t.name}
              </td>
              <td className="mono">{t.profile}</td>
              <td>{flakyReasons(t, data.recent_builds).join("; ")}</td>
              <td>
                <span className="breakdown mono">
                  {t.examples.map((e) => (
                    <Link
                      key={e.build_id}
                      to={`/builds/${e.build_id}?tab=tests`}
                      title={`${e.outcome} at ${shortSha(e.commit_sha)}, ${formatUtc(e.created_at)}`}
                    >
                      #{e.build_number}{" "}
                      <span className={e.outcome === "passed" ? "tone--passed" : "tone--failed"}>{e.outcome}</span>
                    </Link>
                  ))}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
