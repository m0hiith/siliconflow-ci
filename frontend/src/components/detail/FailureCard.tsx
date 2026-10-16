import { Link } from "react-router-dom";
import type { BuildDetail, Stage } from "../../api/client";
import { useLogHead, usePreviousPassing, useTests } from "../../api/queries";
import { shortSha } from "../../format";

/** Why a failed build failed, from the stored log index, JUnit and history. */
export function FailureCard(props: {
  build: BuildDetail;
  stages: Stage[];
  openLog: (stage: string, line?: number) => void;
  openTests: () => void;
}) {
  const { build } = props;
  const failedStage = props.stages.find((s) => s.status === "failed");
  const head = useLogHead(build.id, failedStage?.name);
  const tests = useTests(build);
  const previous = usePreviousPassing(build, true);

  const failing = (tests.data ?? []).filter((t) => t.outcome === "failed" || t.outcome === "error");
  const first = head.data?.first_error ?? null;
  const prev = previous.data;

  return (
    <section className="failure" aria-label="failure summary">
      <div className="failure__head">
        <span className="failure__title">Failed</span>
        {failedStage && (
          <span className="mono">
            in <button type="button" className="linkish" onClick={() => props.openLog(failedStage.name)}>{failedStage.name}</button>
            {failedStage.exit_code != null && <span className="muted"> (exit {failedStage.exit_code})</span>}
          </span>
        )}
        <span className="chip-static mono" title="failure class, from the classified stage log">
          {build.failure_class ?? "unclassified"}
        </span>
      </div>

      <dl className="failure__grid">
        <dt>First error</dt>
        <dd>
          {first ? (
            <>
              <div className="mono failure__msg">{first.message}</div>
              <div className="failure__loc mono">
                {first.url ? (
                  <a href={first.url} target="_blank" rel="noreferrer" title="the file at this build's commit">
                    {first.path}:{first.line}
                    {first.column != null && `:${first.column}`}
                  </a>
                ) : (
                  first.file && (
                    <span title="not a path inside the repository workspace, so not linked">
                      {first.file}
                      {first.line != null && `:${first.line}`}
                    </span>
                  )
                )}
                {failedStage && (
                  <button
                    type="button"
                    className="linkish"
                    onClick={() => props.openLog(failedStage.name, first.line_number)}
                  >
                    log line {first.line_number}
                  </button>
                )}
              </div>
            </>
          ) : head.isPending && failedStage ? (
            <span className="muted">loading…</span>
          ) : (
            <span className="muted">
              {failedStage ? "No error line was found in the stage log." : "No stage reported a failure."}
            </span>
          )}
        </dd>

        <dt>Failing tests</dt>
        <dd>
          {failing.length === 0 ? (
            <span className="muted">{tests.isPending ? "loading…" : "No failing test cases recorded."}</span>
          ) : (
            <ul className="failure__tests">
              {failing.slice(0, 3).map((t) => (
                <li key={`${t.suite}/${t.name}`}>
                  <span className="mono tone--failed">{t.name}</span>
                  {t.message && <span className="mono muted"> — {t.message.split("\n")[0]}</span>}
                </li>
              ))}
              {failing.length > 3 && (
                <li>
                  <button type="button" className="linkish" onClick={props.openTests}>
                    {failing.length - 3} more
                  </button>
                </li>
              )}
            </ul>
          )}
        </dd>

        <dt>Last passing</dt>
        <dd data-testid="last-passing">
          {previous.isPending ? (
            <span className="muted">loading…</span>
          ) : prev?.build ? (
            <span className="mono">
              <Link to={`/builds/${prev.build.id}`}>#{prev.build.number}</Link> ({shortSha(prev.build.commit_sha)})
              {prev.compare_url && (
                <>
                  ,{" "}
                  <a href={prev.compare_url} target="_blank" rel="noreferrer">
                    {prev.commits_between != null
                      ? `${prev.commits_between} commit${prev.commits_between === 1 ? "" : "s"} between`
                      : "compare"}
                  </a>
                </>
              )}
            </span>
          ) : (
            <span className="muted">
              No earlier passing build of {build.branch ?? build.ref} with profile {build.profile}.
            </span>
          )}
        </dd>
      </dl>
    </section>
  );
}
