import { useState } from "react";

export function CopyButton({ text, label = "copy" }: { text: string; label?: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setState("copied");
    } catch {
      setState("failed");
    }
    setTimeout(() => setState("idle"), 1500);
  };
  return (
    <button type="button" className="btn btn--small" onClick={copy}>
      {state === "copied" ? "copied" : state === "failed" ? "copy failed" : label}
    </button>
  );
}
