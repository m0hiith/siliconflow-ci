import { useCallback } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { ApiError, type BuildDetail as Build, type TestOutcome } from "../api/client";
import { useBuild, useProjects, useStages } from "../api/queries";
import { ArtifactsTab } from "../components/detail/ArtifactsTab";
import { FailureCard } from "../components/detail/FailureCard";
import { LogViewer } from "../components/detail/LogViewer";
import { SizeTab } from "../components/detail/SizeTab";
import { StageTimeline } from "../components/detail/StageTimeline";
import { TEST_OUTCOMES, TestsTab } from "../components/detail/TestsTab";
import { hasVersions, VersionChips } from "../components/detail/VersionChips";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { StatusBadge } from "../components/StatusBadge";
import { buildSeconds, firstLine, formatDuration, formatUtc, shortSha } from "../format";
import { useNow } from "../useNow";

const TABS = ["logs", "tests", "artifacts", "size"] as const;
type Tab = (typeof TABS)[number];

export function BuildDetail() {
  const { buildId = "" } = useParams();
  const build = useBuild(buildId);

  if (build.isPending) return <Loading />;
  if (build.isError) {
    if (build.error instanceof ApiError && build.error.status === 404) {
      return (
        <EmptyState title="Build not found">
          No build has the id <span className="mono">{buildId}</span>. <Link to="/builds">All builds</Link>
        </EmptyState>
      );
    }
    return <ErrorState error={build.error} onRetry={build.refetch} />;
  }
  return <Details build={build.data} />;
}

function Details({ build }: { build: Build }) {
  const [params, setParams] = useSearchParams();
  const stages = useStages(build);
  const stageList = stages.data ?? [];

  const tab: Tab = TABS.find((t) => t === params.get("tab")) ?? "logs";
  const defaultStage = stageList.find((s) => s.status === "failed")?.name ?? stageList[0]?.name;
  const stage = params.get("stage") ?? defaultStage;
  const lineParam = Number(params.get("line"));
  const line = Number.isInteger(lineParam) && lineParam > 0 ? lineParam : undefined;
  const outcomes = params.getAll("outcome").filter((o): o is TestOutcome => (TEST_OUTCOMES as readonly string[]).includes(o));

  const update = useCallback(
    (patch: Record<string, string | string[] | undefined>) => {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          for (const [k, v] of Object.entries(patch)) {
            next.delete(k);
            for (const item of v == null ? [] : Array.isArray(v) ? v : [v]) next.append(k, item);
          }
          return next;
        },
        { replace: true },
      );
    },
    [setParams],
  );
  const openLog = (name: string, at?: number) =>
    update({ tab: "logs", stage: name, line: at != null ? String(at) : undefined });
  const setLine = useCallback((n: number | undefined) => update({ line: n != null ? String(n) : undefined }), [update]);

  return (
    <div className="detail">
      <Header build={build} />

      <section className="panel panel--pad" aria-label="stage timeline">
        {stages.isError ? (
          <ErrorState error={stages.error} onRetry={stages.refetch} />
        ) : stageList.length === 0 ? (
          <div className="muted">
            {stages.isPending ? "Loading stages…" : "No stages recorded yet; they arrive with the run report."}
          </div>
        ) : (
          <StageTimeline stages={stageList} selected={tab === "logs" ? stage : undefined} onSelect={(s) => openLog(s)} />
        )}
      </section>

      {build.status === "failed" && stages.isSuccess && (
        <FailureCard
          build={build}
          stages={stageList}
          openLog={openLog}
          openTests={() => update({ tab: "tests", outcome: ["failed", "error"] })}
        />
      )}

      <section className="panel">
        <div className="tabs" role="tablist">
          {TABS.map((t) => (
            <button
              key={t}
              type="button"
              role="tab"
              aria-selected={tab === t}
              className="tab"
              onClick={() => update({ tab: t, line: undefined })}
            >
              {t}
            </button>
          ))}
          {tab === "logs" && stageList.length > 0 && (
            <div className="tabs__stages" role="group" aria-label="log stage">
              {stageList.map((s) => (
                <button
                  key={s.name}
                  type="button"
                  className={`chip chip--stage`}
                  aria-pressed={s.name === stage}
                  onClick={() => openLog(s.name)}
                >
                  {s.name}
                </button>
              ))}
            </div>
          )}
        </div>
        <div className="tabpanel" role="tabpanel">
          {tab === "logs" &&
            (stage ? (
              <LogViewer
                key={stage}
                buildId={build.id}
                stage={stage}
                stageStatus={stageList.find((s) => s.name === stage)?.status}
                artifactsState={build.artifacts_state}
                line={line}
                onLine={setLine}
              />
            ) : (
              <EmptyState title="No stages to show logs for" />
            ))}
          {tab === "tests" && <TestsTab build={build} outcomes={outcomes} setOutcomes={(o) => update({ outcome: o })} />}
          {tab === "artifacts" && <ArtifactsTab build={build} />}
          {tab === "size" && <SizeTab build={build} />}
        </div>
      </section>
    </div>
  );
}

function Header({ build }: { build: Build }) {
  const now = useNow();
  const seconds = buildSeconds(build, now);
  const project = useProjects().data?.find((p) => p.id === build.project_id);

  return (
    <header className="bhead">
      <div className="bhead__crumbs mono muted">
        <Link to="/builds">builds</Link> / <Link to={`/builds?project=${build.project_id}`}>{project?.slug ?? build.repository}</Link>
      </div>
      <div className="bhead__row">
        <h1 className="bhead__num mono">#{build.number}</h1>
        <StatusBadge status={build.status} />
        <span className="chip-static mono" title="profile">{build.profile}</span>
        <span className="mono" title={build.ref}>
          {build.branch ?? build.ref}
          {build.pr_number != null && <span className="muted"> #{build.pr_number}</span>}
        </span>
        {build.commit_sha &&
          (build.commit_url ? (
            <a className="mono" href={build.commit_url} target="_blank" rel="noreferrer" title={build.commit_sha}>
              {shortSha(build.commit_sha)}
            </a>
          ) : (
            <span className="mono">{shortSha(build.commit_sha)}</span>
          ))}
        <span className="mono" title="duration">
          {seconds == null ? "not started" : formatDuration(seconds)}
        </span>
        {build.run_url && (
          <a className="bhead__run" href={build.run_url} target="_blank" rel="noreferrer">
            Actions run {build.gh_run_id}
            {build.gh_run_attempt && build.gh_run_attempt > 1 ? ` (attempt ${build.gh_run_attempt})` : ""} ↗
          </a>
        )}
      </div>
      <div className="bhead__row bhead__row--sub">
        <span className="bhead__msg" title={build.commit_message ?? undefined}>
          {firstLine(build.commit_message) || <span className="muted">no commit message recorded</span>}
        </span>
        <span className="muted">
          {build.author_login ?? "unknown author"} · {build.trigger.replace("_", " ")} · queued{" "}
          <span title={formatUtc(build.queued_at)}>{formatUtc(build.queued_at)}</span>
        </span>
      </div>
      {(hasVersions(build) || build.status_reason) && (
        <div className="bhead__row bhead__row--chips">
          <VersionChips build={build} />
          {build.status_reason && <span className="muted bhead__reason">{build.status_reason}</span>}
        </div>
      )}
    </header>
  );
}
