import type { ReactNode } from "react";

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="state">
      <div className="state__title">{title}</div>
      {children && <div className="state__body">{children}</div>}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  return (
    <div className="state state--error" role="alert">
      <div className="state__title">Could not load data from the API</div>
      <div className="state__body mono">{error.message}</div>
      {onRetry && (
        <button type="button" className="btn" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function Loading() {
  return <div className="state state--loading">Loading…</div>;
}
