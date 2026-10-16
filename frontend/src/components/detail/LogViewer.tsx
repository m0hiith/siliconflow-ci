import { useQueries } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, type StageStatus } from "../../api/client";
import { LOG_CHUNK, logChunkQuery, useLogHead, useLogSearch } from "../../api/queries";
import { stripAnsi } from "../../format";
import { splitMatches, stepTo } from "../../logNav";
import { EmptyState, ErrorState, Loading } from "../States";

const ROW_PX = 18;

export function LogViewer(props: {
  buildId: string;
  stage: string;
  stageStatus: StageStatus | undefined;
  artifactsState: string | null;
  line: number | undefined;
  onLine: (line: number | undefined) => void;
}) {
  const head = useLogHead(props.buildId, props.stage);

  if (head.isPending) return <Loading />;
  if (head.isError) {
    if (head.error instanceof ApiError && head.error.status === 404) {
      return <EmptyState title="No log for this stage">{missingLogReason(props)}</EmptyState>;
    }
    return <ErrorState error={head.error} onRetry={head.refetch} />;
  }
  return <LogBody {...props} index={head.data} />;
}

function missingLogReason(props: { stageStatus: StageStatus | undefined; artifactsState: string | null }): string {
  if (props.stageStatus === "skipped") return "The stage was skipped, so it produced no log.";
  if (props.stageStatus === "pending" || props.stageStatus === "running") return "The stage has not finished.";
  switch (props.artifactsState) {
    case null:
      return "No run report has arrived for this build; logs come with it.";
    case "pending":
      return "Logs arrive with the run's artifact, which is still being ingested.";
    case "rejected":
    case "unavailable":
      return `The run's artifact was ${props.artifactsState}, so no logs were stored.`;
    default:
      return "The run's artifact did not contain a log for this stage.";
  }
}

function LogBody(props: {
  buildId: string;
  stage: string;
  line: number | undefined;
  onLine: (line: number | undefined) => void;
  index: { total_lines: number; errors: number[]; warnings: number[]; error_count: number; warning_count: number };
}) {
  const { buildId, stage, index, line: active, onLine } = props;
  const scrollRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: index.total_lines,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_PX,
    overscan: 40,
  });
  const items = virtualizer.getVirtualItems();

  // Fetch only the chunks the visible rows fall in.
  const chunkIds = [...new Set(items.map((it) => Math.floor(it.index / LOG_CHUNK)))];
  const chunks = useQueries({ queries: chunkIds.map((c) => logChunkQuery(buildId, stage, c)) });
  const lineAt = (i: number): string | undefined => {
    const c = Math.floor(i / LOG_CHUNK);
    return chunks[chunkIds.indexOf(c)]?.data?.lines[i - c * LOG_CHUNK];
  };

  const [draft, setDraft] = useState("");
  const [q, setQ] = useState("");
  useEffect(() => {
    const id = setTimeout(() => setQ(draft.trim()), 250);
    return () => clearTimeout(id);
  }, [draft]);
  const search = useLogSearch(buildId, stage, q);
  const matches = q && search.data?.query === q ? search.data.matches : [];
  // Enter pressed before the debounced search answered: jump once it does.
  const [jumpPending, setJumpPending] = useState(false);

  const errorSet = useMemo(() => new Set(index.errors), [index.errors]);
  const warningSet = useMemo(() => new Set(index.warnings), [index.warnings]);

  useEffect(() => {
    if (active != null && active >= 1 && active <= index.total_lines) {
      virtualizer.scrollToIndex(active - 1, { align: "center" });
    }
  }, [active, index.total_lines, virtualizer]);

  const go = (list: number[], dir: 1 | -1) => {
    const next = stepTo(list, active, dir);
    if (next != null) onLine(next);
  };
  const matchPos = active != null ? matches.indexOf(active) : -1;

  useEffect(() => {
    if (jumpPending && q && search.data?.query === q) {
      setJumpPending(false);
      const next = stepTo(search.data.matches, active, 1);
      if (next != null) onLine(next);
    }
  }, [jumpPending, q, search.data, active, onLine]);

  return (
    <div className="log">
      <div className="log__toolbar">
        <label className="field log__search">
          <span>search</span>
          <input
            className="mono"
            value={draft}
            placeholder="text in this log"
            spellCheck={false}
            aria-label="search log"
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key !== "Enter") return;
              const term = draft.trim();
              if (term !== q || search.data?.query !== term) {
                setQ(term);
                setJumpPending(true);
              } else {
                go(matches, e.shiftKey ? -1 : 1);
              }
            }}
          />
        </label>
        <span className="mono muted log__count" aria-live="polite">
          {q
            ? search.isFetching && !search.data
              ? "searching…"
              : `${matchPos >= 0 ? `${matchPos + 1}/` : ""}${search.data?.total ?? 0} match${search.data?.total === 1 ? "" : "es"}`
            : ""}
        </span>
        <NavPair label="match" list={matches} go={go} disabled={!matches.length} />
        <span className="log__sep" />
        <NavPair
          label="error"
          list={index.errors}
          go={go}
          count={index.error_count}
          tone="failed"
          disabled={!index.errors.length}
        />
        <NavPair
          label="warning"
          list={index.warnings}
          go={go}
          count={index.warning_count}
          tone="running"
          disabled={!index.warnings.length}
        />
        <span className="mono muted log__total">{index.total_lines} lines</span>
      </div>
      {index.total_lines === 0 ? (
        <EmptyState title="The log is empty" />
      ) : (
        <div className="log__scroll" ref={scrollRef} data-testid="log-scroll">
          <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
            {items.map((it) => {
              const n = it.index + 1;
              const text = lineAt(it.index);
              const cls = errorSet.has(n) ? " log__line--error" : warningSet.has(n) ? " log__line--warning" : "";
              return (
                <div
                  key={it.key}
                  className={`log__line${cls}${n === active ? " log__line--active" : ""}`}
                  style={{ transform: `translateY(${it.start}px)` }}
                  data-line={n}
                >
                  <button type="button" className="log__num" onClick={() => onLine(n)} tabIndex={-1}>
                    {n}
                  </button>
                  <span className="log__text">
                    {text == null ? (
                      <span className="muted">…</span>
                    ) : (
                      splitMatches(stripAnsi(text), q).map((part, i) =>
                        part.match ? <mark key={i}>{part.text}</mark> : part.text,
                      )
                    )}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

function NavPair(props: {
  label: string;
  list: number[];
  go: (list: number[], dir: 1 | -1) => void;
  count?: number;
  tone?: string;
  disabled: boolean;
}) {
  const { label, list, count } = props;
  const capped = count != null && count > list.length;
  return (
    <span className={`navpair${props.tone ? ` tone--${props.tone}` : ""}`}>
      {count != null && (
        <span className="mono navpair__count" title={capped ? `navigating the first ${list.length}` : undefined}>
          {count} {label}
          {count === 1 ? "" : "s"}
        </span>
      )}
      <button
        type="button"
        className="btn btn--small"
        disabled={props.disabled}
        onClick={() => props.go(list, -1)}
        aria-label={`previous ${label}`}
      >
        ↑
      </button>
      <button
        type="button"
        className="btn btn--small"
        disabled={props.disabled}
        onClick={() => props.go(list, 1)}
        aria-label={`next ${label}`}
      >
        ↓
      </button>
    </span>
  );
}
