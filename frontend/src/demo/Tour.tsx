import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useDemo } from "./DemoContext";
import { type BuildIds, TOUR } from "./tourSteps";

const HIGHLIGHT = "tour-target";

/**
 * The guided tour (demo mode only): a panel with Back/Next that replays each
 * step's scenario, opens the page it is about and highlights the part to
 * look at. Everything the pages show is the backend's answer to the replay.
 */
export function Tour() {
  const { demo, tourOpen, setTourOpen, scenarios, start, startError } = useDemo();
  const [index, setIndex] = useState(0);
  const navigate = useNavigate();
  const location = useLocation();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const navigatedFor = useRef<string | null>(null);
  const step = TOUR[index] ?? TOUR[0]!;

  const ids = useMemo<BuildIds>(
    () => Object.fromEntries([...scenarios].map(([name, s]) => [name, s.build_ids])),
    [scenarios],
  );
  const scenario = step.scenario ? scenarios.get(step.scenario.name) : undefined;
  const waitingFor = step.scenario?.after ? scenarios.get(step.scenario.after) : undefined;
  const blocked = waitingFor !== undefined && waitingFor.state === "running";

  // Replay the step's scenario once it may start, unless it already has.
  useEffect(() => {
    if (!tourOpen || !step.scenario || blocked || !scenario) return;
    if (scenario.state === "idle" || scenario.state === "failed") {
      void start(step.scenario.name, step.scenario.speed);
    }
  }, [tourOpen, step, scenario, blocked, start]);

  // Go to the step's page once its build exists; then leave the presenter free to click around.
  const route = step.route?.(ids) ?? null;
  useEffect(() => {
    if (!tourOpen || route == null || navigatedFor.current === step.id) return;
    navigatedFor.current = step.id;
    if (`${location.pathname}${location.search}` !== route) navigate(route);
  }, [tourOpen, route, step.id, location.pathname, location.search, navigate]);

  // Highlight the target as soon as it renders (pages load their data first).
  useEffect(() => {
    if (!tourOpen || !step.target) return;
    let el: Element | null = null;
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    const find = () => {
      const found = document.querySelector(step.target!);
      if (found === el) return;
      el?.classList.remove(HIGHLIGHT);
      el = found;
      if (el) {
        el.classList.add(HIGHLIGHT);
        el.scrollIntoView({ block: "start", behavior: reduce ? "auto" : "smooth" });
      }
    };
    find();
    const id = setInterval(find, 400);
    return () => {
      clearInterval(id);
      el?.classList.remove(HIGHLIGHT);
    };
  }, [tourOpen, step.target, location.pathname, location.search]);

  useEffect(() => {
    if (tourOpen) headingRef.current?.focus();
  }, [tourOpen, index]);

  useEffect(() => {
    if (!tourOpen) return;
    const onKey = (e: KeyboardEvent) => {
      // Arrows move the caret in text fields; anywhere else they move the tour.
      const t = e.target as HTMLElement | null;
      const typing =
        t != null &&
        (t.isContentEditable ||
          t.tagName === "TEXTAREA" ||
          t.tagName === "SELECT" ||
          (t instanceof HTMLInputElement && !["checkbox", "radio", "button"].includes(t.type)));
      if (typing) return;
      if (e.key === "Escape") setTourOpen(false);
      else if (e.key === "ArrowRight") go(1);
      else if (e.key === "ArrowLeft") go(-1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // Keep the panel clear of the page on wide screens (styles.css).
  useEffect(() => {
    document.body.classList.toggle("tour-open", demo && tourOpen);
    return () => document.body.classList.remove("tour-open");
  }, [demo, tourOpen]);

  if (!demo || !tourOpen) return null;

  function go(delta: number) {
    const next = index + delta;
    if (next < 0) return;
    if (next >= TOUR.length) {
      setTourOpen(false);
      setIndex(0);
      navigatedFor.current = null;
      return;
    }
    navigatedFor.current = null;
    setIndex(next);
  }

  const events = step.events
    ? (scenarios.get(step.events.scenario)?.events ?? []).filter((e) => e.step === step.events!.step)
    : [];

  return (
    <aside className="tour" role="dialog" aria-modal="false" aria-labelledby="tour-title">
      <div className="tour__head">
        <span className="tour__label mono">Guided tour</span>
        <span className="tour__count mono" aria-live="polite">
          Step {index + 1} of {TOUR.length}
        </span>
        <button type="button" className="btn btn--ghost btn--small" aria-label="Close tour" onClick={() => setTourOpen(false)}>
          ✕
        </button>
      </div>
      <h2 id="tour-title" className="tour__title" tabIndex={-1} ref={headingRef}>
        {step.title}
      </h2>
      <div className="tour__body">{step.plain}</div>

      {step.scenario && (
        <p className="tour__status mono" aria-live="polite">
          {blocked
            ? "Waiting for the sample history to finish replaying…"
            : scenario?.state === "running"
              ? "Replaying sample inputs through the backend…"
              : scenario?.state === "done"
                ? route == null
                  ? "Replayed. Waiting for the build to appear…"
                  : "Replayed. Everything on the page is the backend’s result."
                : scenario?.state === "failed"
                  ? `Replay failed: ${scenario.error ?? "unknown error"}`
                  : "Starting the replay…"}
          {startError && <span className="tone--failed"> {startError}</span>}
        </p>
      )}

      {step.events && (
        <ol className="tour__events mono" aria-label="deliveries and the server's answers">
          {events.length === 0 && <li className="muted">no deliveries yet</li>}
          {events.map((e, i) => (
            <li key={i}>
              delivery {e.delivery_id?.slice(0, 8)} → HTTP {e.status_code}{" "}
              <span className={e.result === "duplicate" ? "tone--neutral" : "tone--passed"}>{e.result}</span>
            </li>
          ))}
        </ol>
      )}

      <details className="tour__tech">
        <summary>Technical detail</summary>
        {step.technical}
      </details>

      <div className="tour__nav">
        <button type="button" className="btn" onClick={() => go(-1)} disabled={index === 0}>
          Back
        </button>
        <span className="muted tour__keys">← → to move, Esc to close</span>
        <button type="button" className="btn tour__next" onClick={() => go(1)}>
          {index === TOUR.length - 1 ? "Finish" : "Next"}
        </button>
      </div>
    </aside>
  );
}
