import type { BuildStatus } from "../api/client";
import { statusTone } from "../status";

export function StatusBadge({ status }: { status: BuildStatus }) {
  return (
    <span className={`status status--${statusTone(status)}`}>
      <span className="status__dot" aria-hidden />
      {status.replace("_", " ")}
    </span>
  );
}
