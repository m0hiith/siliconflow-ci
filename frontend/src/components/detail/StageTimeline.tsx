import type { Stage } from "../../api/client";
import { formatDuration } from "../../format";
import { stageTone } from "../../status";
import { useNow } from "../../useNow";

function stageSeconds(stage: Stage, now: number): number | null {
  if (!stage.started_at) return null;
  const end = stage.finished_at ? Date.parse(stage.finished_at) : now;
  return Math.max(0, (end - Date.parse(stage.started_at)) / 1000);
}

/**
 * One bar per stage, in pipeline order, its width proportional to its
 * duration. Stages that never ran (skipped, pending) get a fixed narrow slot.
 */
export function StageTimeline(props: {
  stages: Stage[];
  selected: string | undefined;
  onSelect: (stage: string) => void;
}) {
  const now = useNow();
  const seconds = props.stages.map((s) => stageSeconds(s, now));
  const total = seconds.reduce<number>((a, s) => a + (s ?? 0), 0);

  return (
    <div className="timeline" role="list" aria-label="stages">
      {props.stages.map((stage, i) => {
        const s = seconds[i] ?? null;
        const tone = stageTone(stage.status);
        // Every bar is wide enough for its label; the rest of the row is
        // shared in proportion to duration (timestamps are whole seconds).
        const grow = s == null ? 0 : total > 0 ? s / total : 1;
        return (
          <button
            key={stage.name}
            type="button"
            role="listitem"
            className={`seg seg--${tone}${props.selected === stage.name ? " seg--selected" : ""}`}
            style={s == null ? { flex: "0 0 auto" } : { flex: `${grow} 1 auto` }}
            onClick={() => props.onSelect(stage.name)}
            title={`${stage.name}: ${stage.status}${stage.exit_code != null ? ` (exit ${stage.exit_code})` : ""}${stage.required ? "" : " · optional"}`}
            aria-label={`${stage.name} ${stage.status}${s != null ? ` ${formatDuration(s)}` : ""}`}
            data-stage={stage.name}
          >
            <span className="seg__bar" />
            <span className="seg__label">
              <span className="seg__name">{stage.name}</span>
              <span className="seg__time mono">{s == null ? stage.status : formatDuration(s)}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}
