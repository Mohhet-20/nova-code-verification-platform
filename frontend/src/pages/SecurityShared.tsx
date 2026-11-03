import { Badge } from "../components/ui";
import type { SecPriority, SecSeverity, SecStatus } from "../types/api";

type Tone = "success" | "danger" | "warning" | "info" | "neutral";
export const SEV_TONE: Record<string, Tone> = { critical: "danger", high: "danger", medium: "warning", low: "info", info: "neutral" };
export const STATUS_TONE: Record<SecStatus, Tone> = { open: "warning", resolved: "success", false_positive: "neutral", accepted_risk: "neutral", suppressed: "neutral" };
export const DECISION_TONE: Record<string, Tone> = { pass: "success", fail: "danger", indeterminate: "warning" };

export const SeverityBadge = ({ severity }: { severity: SecSeverity | string }) => <Badge tone={SEV_TONE[severity] ?? "neutral"}>{severity}</Badge>;
export const StatusBadge = ({ status }: { status: SecStatus }) => <Badge tone={STATUS_TONE[status]}>{status.replace("_", " ")}</Badge>;
export const PriorityBadge = ({ priority }: { priority: SecPriority | undefined }) => (priority ? <Badge tone={priority.bucket === "P1" ? "danger" : priority.bucket === "P2" ? "warning" : "neutral"}>{priority.bucket}</Badge> : null);

/** The exact backend decision. Indeterminate is never presented as a pass. */
export function DecisionBadge({ status }: { status: "pass" | "fail" | "indeterminate" | null | undefined }) {
  if (!status) return <Badge>No decision</Badge>;
  return <Badge tone={DECISION_TONE[status]}>{status === "indeterminate" ? "INDETERMINATE" : status.toUpperCase()}</Badge>;
}

export const ScanStateBadge = ({ state }: { state: string | undefined }) => (
  <Badge tone={state === "complete" ? "success" : state === "failed" ? "danger" : "warning"}>{state === "complete" ? "Complete scan" : state === "failed" ? "Scan failed" : "PARTIAL scan"}</Badge>
);
