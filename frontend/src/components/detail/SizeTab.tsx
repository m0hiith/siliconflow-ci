import { Link } from "react-router-dom";
import type { BuildDetail } from "../../api/client";
import { useBuildSize } from "../../api/queries";
import { shortSha } from "../../format";
import { formatDelta, formatPercent, sectionRows, totalRows, type SizeRow } from "../../size";
import { EmptyState, ErrorState, Loading } from "../States";

export function SizeTab({ build }: { build: BuildDetail }) {
  const query = useBuildSize(build);
  if (query.isPending) return <Loading />;
  if (query.isError) return <ErrorState error={query.error} onRetry={query.refetch} />;
  const { size, base_build: baseBuild, base_size: baseSize, base_branch: baseBranch } = query.data;

  if (!size) {
    return (
      <EmptyState title="No size reported">
        {build.artifacts_state == null
          ? "No run report has arrived for this build."
          : "This build's run report has no size: its profile does not run the size stage, or the stage failed."}
      </EmptyState>
    );
  }

  return (
    <>
      <div className="panel__head">
        <h2 className="mono">{size.file_name}</h2>
        <span className="muted" data-testid="size-base">
          {baseBuild ? (
            <>
              compared with <Link to={`/builds/${baseBuild.id}?tab=size`}>#{baseBuild.number}</Link>
              {baseBuild.commit_sha && <span className="mono"> {shortSha(baseBuild.commit_sha)}</span>}, the latest
              passing {baseBranch} build of {build.profile} before this one
            </>
          ) : (
            `no earlier passing ${baseBranch} build of ${build.profile} reported a size, so there is nothing to compare with`
          )}
        </span>
      </div>
      <SizeTable label="size totals" rows={totalRows(size, baseSize)} compared={!!baseSize} />
      {size.sections.length > 0 && (
        <>
          <div className="panel__head">
            <h2>Sections</h2>
            <span className="muted">as size -A lists them, including sections that are not loaded</span>
          </div>
          <SizeTable label="sections" rows={sectionRows(size, baseSize)} compared={!!baseSize} />
        </>
      )}
    </>
  );
}

function SizeTable({ label, rows, compared }: { label: string; rows: SizeRow[]; compared: boolean }) {
  return (
    <table className="table table--fixed" aria-label={label}>
      <thead>
        <tr>
          <th>Name</th>
          <th className="num">Bytes</th>
          {compared && (
            <>
              <th className="num">Base</th>
              <th className="num">Delta</th>
              <th className="num">Change</th>
            </>
          )}
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.name}>
            <td className="mono truncate" title={r.name}>{r.name}</td>
            <td className="num mono">{r.bytes?.toLocaleString("en-US")}</td>
            {compared && (
              <>
                <td className="num mono">{r.base?.toLocaleString("en-US")}</td>
                <td className={`num mono ${tone(r.delta)}`}>{formatDelta(r.delta)}</td>
                <td className={`num mono ${tone(r.delta)}`}>{formatPercent(r.percent)}</td>
              </>
            )}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Growth is the thing to notice; shrinking is good news. */
function tone(delta: number | null): string {
  if (!delta) return "muted";
  return delta > 0 ? "tone--failed" : "tone--passed";
}
