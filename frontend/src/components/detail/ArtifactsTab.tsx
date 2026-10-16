import type { BuildDetail, GateResult } from "../../api/client";
import { useArtifacts, useGates } from "../../api/queries";
import { formatBytes, shortSha } from "../../format";
import { CopyButton } from "../CopyButton";
import { EmptyState, ErrorState, Loading } from "../States";
import { hasVersions, VersionChips } from "./VersionChips";

const ARTIFACT_STATE: Record<string, string> = {
  none: "The run uploaded no artifact.",
  pending: "The run's artifact is still being downloaded and verified.",
  rejected: "The run's artifact was rejected: a file disagreed with its manifest, so nothing was stored.",
  unavailable: "The run's artifact had expired or was too large to fetch.",
};

export function ArtifactsTab({ build }: { build: BuildDetail }) {
  const artifacts = useArtifacts(build);
  if (artifacts.isPending) return <Loading />;
  if (artifacts.isError) return <ErrorState error={artifacts.error} onRetry={artifacts.refetch} />;
  const items = artifacts.data;

  // One command checks every file against the hashes the server computed.
  const verify = ["sha256sum -c <<'EOF'", ...items.map((a) => `${a.sha256}  ${a.file_name}`), "EOF"].join("\n");

  return (
    <>
      <Provenance build={build} />
      {items.length === 0 ? (
        <EmptyState title="No artifacts">
          {(build.artifacts_state && ARTIFACT_STATE[build.artifacts_state]) ??
            (build.artifacts_state == null
              ? "No run report has arrived for this build."
              : "The run's artifact contained no firmware files (the package stage did not produce any).")}
        </EmptyState>
      ) : (
        <>
          <table className="table table--fixed" aria-label="artifacts">
            <colgroup>
              <col style={{ width: "22%" }} />
              <col style={{ width: 64 }} />
              <col style={{ width: 96 }} />
              <col />
              <col style={{ width: 120 }} />
            </colgroup>
            <thead>
              <tr>
                <th>File</th>
                <th>Kind</th>
                <th className="num">Size</th>
                <th>SHA-256 (computed by the server)</th>
                <th>Release</th>
              </tr>
            </thead>
            <tbody>
              {items.map((a) => (
                <tr key={a.id}>
                  <td className="mono truncate">
                    <a href={`/api/v1/artifacts/${a.id}/download`} title={`download ${a.file_name}`}>
                      {a.file_name}
                    </a>
                  </td>
                  <td className="mono">{a.kind}</td>
                  <td className="num mono" title={`${a.size_bytes} bytes`}>
                    {formatBytes(a.size_bytes)}
                  </td>
                  <td className="mono sha-cell">
                    <div>
                      <span className="truncate" title={a.sha256}>{a.sha256}</span>
                      <CopyButton text={a.sha256} />
                    </div>
                  </td>
                  <td className={`mono ${a.release_eligible ? "tone--passed" : "muted"}`}>
                    {a.release_eligible ? "eligible" : "not eligible"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="verify">
            <div className="verify__head">
              <span>Verify a download</span>
              <CopyButton text={verify} label="copy command" />
            </div>
            <pre className="mono" data-testid="verify-command">{verify}</pre>
          </div>
        </>
      )}
      <Gates build={build} />
    </>
  );
}

/** Where the artifacts came from: the commit, the run that built them and its tools. */
function Provenance({ build }: { build: BuildDetail }) {
  return (
    <section className="provenance" aria-label="provenance">
      <dl className="provenance__facts">
        <dt>Commit</dt>
        <dd className="mono">
          {build.commit_sha ? (
            build.commit_url ? (
              <a href={build.commit_url} target="_blank" rel="noreferrer" title={build.commit_sha}>
                {shortSha(build.commit_sha)}
              </a>
            ) : (
              <span title={build.commit_sha}>{shortSha(build.commit_sha)}</span>
            )
          ) : (
            <span className="muted">unknown</span>
          )}{" "}
          <span className="muted" title={build.ref}>{build.ref}</span>
        </dd>
        <dt>Built by</dt>
        <dd className="mono">
          {build.run_url ? (
            <a href={build.run_url} target="_blank" rel="noreferrer">
              run {build.gh_run_id}
              {build.gh_run_attempt && build.gh_run_attempt > 1 ? ` attempt ${build.gh_run_attempt}` : ""}
            </a>
          ) : (
            <span className="muted">no Actions run recorded</span>
          )}{" "}
          <span className="muted">profile</span> {build.profile}
        </dd>
        <dt>Versions</dt>
        <dd className="chips">
          {hasVersions(build) ? (
            <VersionChips build={build} />
          ) : (
            <span className="muted">
              {build.artifacts_state == null ? "No run report has arrived for this build." : "The run report named no versions."}
            </span>
          )}
        </dd>
      </dl>
    </section>
  );
}

function Gates({ build }: { build: BuildDetail }) {
  const gates = useGates(build);
  if (gates.isPending) return <Loading />;
  if (gates.isError) return <ErrorState error={gates.error} onRetry={gates.refetch} />;
  const results = gates.data;
  const failed = results.filter((g) => !g.passed);

  return (
    <section className="gates" aria-label="release gates">
      <div className="panel__head">
        <h2>Release gates</h2>
        {results.length > 0 && (
          <span className={failed.length ? "tone--failed" : "tone--passed"}>
            {failed.length
              ? `not release-eligible: ${failed.length} of ${results.length} checks failed`
              : `release-eligible: all ${results.length} checks passed`}
          </span>
        )}
      </div>
      {results.length === 0 ? (
        <EmptyState title="Gates not evaluated">
          {build.status === "passed"
            ? "Gates are evaluated once the build has passed and its artifacts are stored."
            : "Gates are evaluated only for passed builds, so this build is not release-eligible."}
        </EmptyState>
      ) : (
        <table className="table table--fixed">
          <colgroup>
            <col style={{ width: "18%" }} />
            <col style={{ width: 64 }} />
            <col style={{ width: "18%" }} />
            <col style={{ width: "18%" }} />
            <col />
          </colgroup>
          <thead>
            <tr>
              <th>Check</th>
              <th>Result</th>
              <th>Observed</th>
              <th>Threshold</th>
              <th>Message</th>
            </tr>
          </thead>
          <tbody>
            {results.map((g) => (
              <tr key={g.rule_id}>
                <td className="mono truncate" title={g.rule_id}>{g.rule_id}</td>
                <td className={`mono ${g.passed ? "tone--passed" : "tone--failed"}`}>{g.passed ? "pass" : "fail"}</td>
                <td className="mono wrap">{show(g.observed)}</td>
                <td className="mono wrap">{show(g.threshold)}</td>
                <td className="wrap">{g.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function show(value: GateResult["observed"]): string {
  if (value == null) return "—";
  if (typeof value === "object") {
    return Array.isArray(value)
      ? value.join(", ")
      : Object.entries(value as Record<string, unknown>).map(([k, v]) => `${k}: ${String(v)}`).join(", ");
  }
  return String(value);
}
