import { useQueryClient } from "@tanstack/react-query";
import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { DemoScenario } from "../api/client";
import { startDemoScenario, useDemoScenarios, useMeta } from "../api/queries";

/**
 * Demo mode (docs/demo.md): whether this instance is one, its scenarios, the
 * guided tour and the plain-language toggle. Outside demo mode `demo` is
 * false and nothing here changes what the UI shows.
 */
interface DemoState {
  demo: boolean;
  scenarios: Map<string, DemoScenario>;
  start: (name: string, speed: number) => Promise<void>;
  startError: string | null;
  plain: boolean;
  setPlain: (on: boolean) => void;
  tourOpen: boolean;
  setTourOpen: (open: boolean) => void;
}

const DemoContext = createContext<DemoState>({
  demo: false,
  scenarios: new Map(),
  start: async () => {},
  startError: null,
  plain: false,
  setPlain: () => {},
  tourOpen: false,
  setTourOpen: () => {},
});

const PLAIN_KEY = "sfci.demo.plain";

function readPlain(): boolean {
  try {
    return localStorage.getItem(PLAIN_KEY) === "1";
  } catch {
    return false;
  }
}

export function DemoProvider({ children }: { children: ReactNode }) {
  const meta = useMeta();
  const demo = meta.data?.demo_mode === true;
  const scenarios = useDemoScenarios(demo);
  const queryClient = useQueryClient();
  const [plain, setPlainState] = useState(readPlain);
  const [tourOpen, setTourOpen] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  const running = scenarios.data?.some((s) => s.state === "running") ?? false;
  // A replay creates builds the open page has not seen; refresh while it runs.
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => {
      for (const key of ["builds", "build", "metrics", "analytics"]) {
        void queryClient.invalidateQueries({ queryKey: [key] });
      }
    }, 1500);
    return () => clearInterval(id);
  }, [running, queryClient]);

  const start = useCallback(
    async (name: string, speed: number) => {
      try {
        await startDemoScenario(name, speed);
        setStartError(null);
      } catch (e) {
        setStartError(e instanceof Error ? e.message : String(e));
      }
      await queryClient.invalidateQueries({ queryKey: ["demo", "scenarios"] });
    },
    [queryClient],
  );

  const setPlain = useCallback((on: boolean) => {
    setPlainState(on);
    try {
      localStorage.setItem(PLAIN_KEY, on ? "1" : "0");
    } catch {
      // Not remembered; the toggle still works for this page.
    }
  }, []);

  const value = useMemo<DemoState>(
    () => ({
      demo,
      scenarios: new Map((scenarios.data ?? []).map((s) => [s.name, s])),
      start,
      startError,
      plain: demo && plain,
      setPlain,
      tourOpen: demo && tourOpen,
      setTourOpen,
    }),
    [demo, scenarios.data, start, startError, plain, setPlain, tourOpen],
  );
  return <DemoContext.Provider value={value}>{children}</DemoContext.Provider>;
}

export function useDemo(): DemoState {
  return useContext(DemoContext);
}

/**
 * `term(jargon, plain)`: the plain-language label when the demo's toggle is
 * on, else the usual one. Outside demo mode it always returns `jargon`.
 */
export function useTerm(): (jargon: string, plain: string) => string {
  const { plain } = useDemo();
  return useCallback((jargon: string, simple: string) => (plain ? simple : jargon), [plain]);
}
