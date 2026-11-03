import { Badge } from "../components/ui";
import type { CvFreshness, GateOutcome } from "../types/api";

type Tone = "success" | "danger" | "warning" | "info" | "neutral";
export const GATE_TONE: Record<GateOutcome, Tone> = { PASS: "success", FAIL: "danger", BLOCKED: "warning", INCOMPLETE: "warning" };
export const RUN_TONE: Record<string, Tone> = { queued: "info", running: "info", completed: "neutral", cancelled: "warning", error: "danger" };
export const CHECK_TONE: Record<string, Tone> = {
  planned: "neutral", running: "info", passed: "success", failed: "danger", blocked: "warning", incomplete: "warning", error: "danger", cancelled: "warning", not_applicable: "neutral",
  pass: "success", fail: "danger", "n/a": "neutral",
};
export const CATEGORY_TONE: Record<string, Tone> = { new: "danger", pre_existing: "warning", flaky: "warning", unclassified: "warning", resolved: "success" };

/** The exact backend outcome, never upgraded: a run without a decision shows its run status instead. */
export function GateBadge({ outcome, status, freshness }: { outcome: GateOutcome | null; status?: string; freshness?: CvFreshness }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      {outcome ? <Badge tone={GATE_TONE[outcome]}>{outcome}</Badge> : <Badge tone={RUN_TONE[status ?? ""] ?? "neutral"}>{status ? `No decision (${status})` : "No decision"}</Badge>}
      {freshness?.stale === true && <Badge tone="warning">Stale</Badge>}
    </span>
  );
}

export const shortSha = (s: string | null | undefined) => (s ? s.slice(0, 8) : "—");
