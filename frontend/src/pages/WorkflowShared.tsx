import { Badge } from "../components/ui";
import type { CiReadiness } from "../types/api";

type Tone = "success" | "danger" | "warning" | "info" | "neutral";
export const READY_TONE: Record<CiReadiness, Tone> = { READY: "success", NOT_READY: "danger", BLOCKED: "warning", INCOMPLETE: "warning" };
export const REPORT_TONE: Record<string, Tone> = { sent: "success", pending: "info", unconfigured: "warning", retry_scheduled: "warning", failed_permanent: "danger", skipped_stale: "neutral" };

/** The exact backend readiness, never upgraded. No decision is shown as such, not as ready. */
export function ReadinessBadge({ outcome, stale }: { outcome: CiReadiness | null | undefined; stale?: boolean }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      {outcome ? <Badge tone={READY_TONE[outcome]}>{outcome}</Badge> : <Badge>No decision</Badge>}
      {stale === true && <Badge tone="warning">Stale</Badge>}
    </span>
  );
}

export function download(name: string, text: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}
