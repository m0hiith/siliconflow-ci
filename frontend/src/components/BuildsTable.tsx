import { Link, useNavigate } from "react-router-dom";
import type { Build } from "../api/client";
import { useProjects } from "../api/queries";
import { buildSeconds, formatAgo, formatDuration, formatUtc, shortSha } from "../format";
import { isTerminal } from "../status";
import { useNow } from "../useNow";
import { StatusBadge } from "./StatusBadge";

export function BuildsTable({ builds }: { builds: Build[] }) {
  const now = useNow();
  const navigate = useNavigate();
  const projects = useProjects().data;
  const slugs = new Map(projects?.map((p) => [p.id, p.slug]));

  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th className="num">#</th>
            <th>Status</th>
            <th>Project</th>
            <th>Branch</th>
            <th>Profile</th>
            <th>Commit</th>
            <th>Author</th>
            <th>Failure</th>
            <th className="num">Duration</th>
            <th>Queued</th>
          </tr>
        </thead>
        <tbody>
          {builds.map((b) => {
            const seconds = buildSeconds(b, now);
            return (
              <tr
                key={b.id}
                className="row-link"
                title={b.status_reason ?? undefined}
                onClick={(e) => {
                  // Leave text selection, modified clicks and inner links alone.
                  if (e.defaultPrevented || e.metaKey || e.ctrlKey || window.getSelection()?.toString()) return;
                  if ((e.target as HTMLElement).closest("a")) return;
                  navigate(`/builds/${b.id}`);
                }}
              >
                <td className="num mono" title={b.id}>
                  <Link to={`/builds/${b.id}`}>{b.number}</Link>
                </td>
                <td>
                  <StatusBadge status={b.status} />
                </td>
                <td className="mono">{slugs.get(b.project_id) ?? b.project_id}</td>
                <td className="mono truncate" title={b.ref}>
                  {b.branch ?? b.ref}
                  {b.pr_number != null && <span className="muted"> #{b.pr_number}</span>}
                </td>
                <td className="mono">{b.profile}</td>
                <td className="commit">
                  <span className="mono sha" title={b.commit_sha ?? undefined}>
                    {shortSha(b.commit_sha) || "—"}
                  </span>
                  <span className="truncate muted">{b.commit_message?.split("\n")[0]}</span>
                </td>
                <td className="truncate">{b.author_login ?? b.trigger}</td>
                <td className="mono">{b.failure_class ?? ""}</td>
                <td className={`num mono${isTerminal(b.status) ? "" : " live"}`}>
                  {seconds == null ? "—" : formatDuration(seconds)}
                </td>
                <td className="nowrap" title={formatUtc(b.queued_at)}>
                  {formatAgo(b.queued_at, now)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
