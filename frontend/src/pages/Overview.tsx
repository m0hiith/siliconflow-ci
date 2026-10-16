import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import type { MetricsSummary } from "../api/client";
import { useMetricsSummary } from "../api/queries";
import { BuildsTable } from "../components/BuildsTable";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { formatDuration, formatPercent, formatUtc } from "../format";
import { BUILD_STATUSES, statusTone } from "../status";

export function Overview() {
  const summary = useMetricsSummary();

  if (summary.isPending) return <Loading />;
  if (summary.isError) return <ErrorState error={summary.error} onRetry={summary.refetch} />;
  const m = summary.data;

  return (
    <>
      <div className="page-head">
        <h1>Overview</h1>
        <span className="page-head__meta mono">
          today = since {formatUtc(m.window_start)}
          {m.active_builds > 0 && (
            <Link className="live-pill" to="/builds?status=queued&status=running">
              {m.active_builds} active
            </Link>
          )}
        </span>
      </div>

      <section className="tiles">
        <Tile label="Builds today" value={String(m.builds_today)}>
          {m.builds_today === 0 ? "no builds recorded today" : <StatusBreakdown m={m} />}
        </Tile>
        <Tile
          label="Pass rate"
          value={m.pass_rate == null ? "—" : formatPercent(m.pass_rate)}
        >
          {m.pass_rate == null
            ? "no passed or failed builds today"
            : `${m.status_counts.passed ?? 0} passed / ${m.failed_today} failed`}
        </Tile>
        <Tile
          label="Median duration"
          value={m.median_duration_seconds == null ? "—" : formatDuration(m.median_duration_seconds)}
        >
          {m.median_duration_seconds == null
            ? "no finished builds today"
            : "passed + failed builds, start to finish"}
        </Tile>
        <Tile
          label="Failed today"
          value={String(m.failed_today)}
          tone={m.failed_today > 0 ? "failed" : undefined}
        >
          {m.failed_today > 0 ? (
            <Link to="/builds?status=failed">show failed builds</Link>
          ) : m.builds_today === 0 ? (
            "nothing ran today"
          ) : (
            "no failures today"
          )}
        </Tile>
      </section>

      <section className="panel">
        <div className="panel__head">
          <h2>Recent failures</h2>
          {m.recent_failures.length > 0 && (
            <span className="muted">latest {m.recent_failures.length} failed builds, any day</span>
          )}
        </div>
        {m.recent_failures.length === 0 ? (
          <EmptyState title="No failed builds recorded">
            Failures appear here once a run report marks a build as failed.
          </EmptyState>
        ) : (
          <BuildsTable builds={m.recent_failures} />
        )}
      </section>
    </>
  );
}

function Tile(props: { label: string; value: string; tone?: string; children: ReactNode }) {
  return (
    <div className="tile">
      <div className="tile__label">{props.label}</div>
      <div className={`tile__value mono${props.tone ? ` tone--${props.tone}` : ""}`}>
        {props.value}
      </div>
      <div className="tile__sub">{props.children}</div>
    </div>
  );
}

function StatusBreakdown({ m }: { m: MetricsSummary }) {
  return (
    <span className="breakdown">
      {BUILD_STATUSES.filter((s) => (m.status_counts[s] ?? 0) > 0).map((s) => (
        <span key={s} className={`tone--${statusTone(s)}`}>
          {m.status_counts[s]} {s.replace("_", " ")}
        </span>
      ))}
    </span>
  );
}
