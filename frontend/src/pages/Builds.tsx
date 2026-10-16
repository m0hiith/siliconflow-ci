import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import type { BuildStatus } from "../api/client";
import { useBuilds, useProjects } from "../api/queries";
import { BuildsTable } from "../components/BuildsTable";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { type BuildFilters, hasFilters, parseFilters, toSearchParams } from "../filters";
import { BUILD_STATUSES, statusTone } from "../status";

export function Builds() {
  const [params, setParams] = useSearchParams();
  const filters = parseFilters(params);
  const builds = useBuilds(filters);
  const items = builds.data?.pages.flatMap((p) => p.items) ?? [];

  const update = (patch: Partial<BuildFilters>) =>
    setParams(toSearchParams({ ...filters, ...patch }), { replace: true });

  return (
    <>
      <div className="page-head">
        <h1>Builds</h1>
        <span className="page-head__meta mono">
          {builds.isSuccess && `${items.length}${builds.hasNextPage ? "+" : ""} shown`}
          {builds.isFetching && !builds.isPending && <span className="live-dot" title="refreshing" />}
        </span>
      </div>

      <FilterBar filters={filters} update={update} clear={() => setParams({}, { replace: true })} />

      <section className="panel">
        {builds.isPending ? (
          <Loading />
        ) : builds.isError ? (
          <ErrorState error={builds.error} onRetry={builds.refetch} />
        ) : items.length === 0 ? (
          hasFilters(filters) ? (
            <EmptyState title="No builds match these filters">
              Branch and profile match exactly.
            </EmptyState>
          ) : (
            <EmptyState title="No builds recorded yet">
              Builds appear when GitHub sends a workflow_run webhook or a manual build is started.
            </EmptyState>
          )
        ) : (
          <>
            <BuildsTable builds={items} />
            {builds.hasNextPage && (
              <div className="more">
                <button
                  type="button"
                  className="btn"
                  disabled={builds.isFetchingNextPage}
                  onClick={() => builds.fetchNextPage()}
                >
                  {builds.isFetchingNextPage ? "Loading…" : "Load older builds"}
                </button>
              </div>
            )}
          </>
        )}
      </section>
    </>
  );
}

function FilterBar(props: {
  filters: BuildFilters;
  update: (patch: Partial<BuildFilters>) => void;
  clear: () => void;
}) {
  const { filters, update } = props;
  const projects = useProjects();

  const toggle = (s: BuildStatus) =>
    update({
      status: filters.status.includes(s)
        ? filters.status.filter((x) => x !== s)
        : [...filters.status, s],
    });

  return (
    <div className="filters">
      <label className="field">
        <span>project</span>
        <select
          value={filters.project ?? ""}
          onChange={(e) => update({ project: e.target.value || undefined })}
        >
          <option value="">all</option>
          {projects.data?.map((p) => (
            <option key={p.id} value={p.id}>
              {p.slug}
            </option>
          ))}
          {/* Keep an id from the URL selectable even if it is not a known project. */}
          {filters.project && projects.data && !projects.data.some((p) => p.id === filters.project) && (
            <option value={filters.project}>{filters.project}</option>
          )}
        </select>
      </label>
      <TextFilter label="branch" value={filters.branch} onCommit={(branch) => update({ branch })} />
      <TextFilter label="profile" value={filters.profile} onCommit={(profile) => update({ profile })} />
      <div className="chips" role="group" aria-label="status">
        {BUILD_STATUSES.map((s) => (
          <button
            key={s}
            type="button"
            className={`chip chip--${statusTone(s)}`}
            aria-pressed={filters.status.includes(s)}
            onClick={() => toggle(s)}
          >
            {s.replace("_", " ")}
          </button>
        ))}
      </div>
      {hasFilters(filters) && (
        <button type="button" className="btn btn--ghost" onClick={props.clear}>
          clear
        </button>
      )}
    </div>
  );
}

/** A text input that writes to the URL on Enter or blur, not on every keystroke. */
function TextFilter(props: {
  label: string;
  value: string | undefined;
  onCommit: (value: string | undefined) => void;
}) {
  const [draft, setDraft] = useState(props.value ?? "");
  useEffect(() => setDraft(props.value ?? ""), [props.value]);

  const commit = () => {
    const next = draft.trim() || undefined;
    if (next !== props.value) props.onCommit(next);
  };

  return (
    <label className="field">
      <span>{props.label}</span>
      <input
        className="mono"
        value={draft}
        placeholder="any"
        spellCheck={false}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => e.key === "Enter" && commit()}
      />
    </label>
  );
}
