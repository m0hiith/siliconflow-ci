import { useDemo } from "./DemoContext";

export const DEMO_BANNER_TEXT =
  "Demo mode — replayed sample inputs, processed by the real SiliconFlow backend. Not production results.";

/** Shown on every page in demo mode, above everything, with no way to hide it. */
export function DemoBanner() {
  const { demo, plain, setPlain, tourOpen, setTourOpen } = useDemo();
  if (!demo) return null;
  return (
    <div className="demo-banner" role="status" data-testid="demo-banner">
      <span className="demo-banner__tag mono">DEMO</span>
      <span className="demo-banner__text">{DEMO_BANNER_TEXT}</span>
      <span className="demo-banner__actions">
        <label className="demo-switch">
          <input
            type="checkbox"
            role="switch"
            checked={plain}
            aria-checked={plain}
            onChange={(e) => setPlain(e.target.checked)}
          />
          Plain language
        </label>
        {!tourOpen && (
          <button type="button" className="btn demo-banner__tour" onClick={() => setTourOpen(true)}>
            Start tour
          </button>
        )}
      </span>
    </div>
  );
}
