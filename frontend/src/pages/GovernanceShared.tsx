import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { Badge, EmptyState, ErrorState, Notice, Spinner, cx } from "../components/ui";
import { ApiError } from "../services/api";
import { govApi } from "../services/governanceApi";
import type { GovMe, PolicyStatus, WorkflowState } from "../types/governance";

const LINKS = [
  ["/governance", "Overview", true], ["/governance/approvals", "My approvals", false], ["/governance/queue", "Review queue", false], ["/governance/teams", "Teams", false],
  ["/governance/policies", "Policies", false], ["/governance/audit", "Audit", false],
] as const;

export function GovernanceNav() {
  return (
    <nav aria-label="Governance" className="mb-6 flex flex-wrap gap-1 border-b border-slate-200 pb-2">
      {LINKS.map(([to, label, end]) => (
        <NavLink key={to} to={to} end={end} className={({ isActive }) => cx("rounded-md px-3 py-1.5 text-sm font-medium", isActive ? "bg-sky-100 text-sky-900" : "text-slate-600 hover:bg-slate-100")}>
          {label}
        </NavLink>
      ))}
    </nav>
  );
}

export function useMe(): UseQueryResult<GovMe> {
  return useQuery({ queryKey: ["gov-me"], queryFn: govApi.me, staleTime: 60_000 });
}

/** True only when the server-reported permission set allows it. This hides controls; it never grants anything: the API re-checks every request. */
export function can(me: GovMe | undefined, permission: string, teamId?: string | null): boolean {
  if (!me) return false;
  if (me.org_permissions.includes("*") || me.org_permissions.includes(permission)) return true;
  return !!teamId && me.teams.some((t) => t.team_id === teamId && t.permissions.includes(permission));
}

export function PermissionDenied({ what = "this page" }: { what?: string }) {
  return (
    <div role="alert" className="rounded-lg border border-amber-200 bg-amber-50 p-5 text-sm text-amber-900" data-testid="permission-denied">
      <p className="font-semibold">You don't have access to {what}</p>
      <p className="mt-1">Your role does not permit this. If you think it should, ask an organization or team administrator. No data was changed.</p>
    </div>
  );
}

/** One place that renders loading / permission-denied / unavailable / error for a query, so every screen handles them the same way. */
export function Guard<T>({ query, what, children }: { query: UseQueryResult<T>; what?: string; children: (data: T) => ReactNode }) {
  if (query.isPending) return <Spinner />;
  if (query.isError) {
    const e = query.error;
    if (e instanceof ApiError && e.status === 403) return <PermissionDenied what={what} />;
    if (e instanceof ApiError && e.status === 404) return <EmptyState title="Not available">This item does not exist or you cannot see it.</EmptyState>;
    return <ErrorState error={e} onRetry={() => void query.refetch()} />;
  }
  return <>{children(query.data)}</>;
}

const STATE_TONE: Record<WorkflowState, "neutral" | "info" | "success" | "warning" | "danger"> = {
  DRAFT: "neutral", SUBMITTED: "info", IN_REVIEW: "info", CHANGES_REQUESTED: "warning", APPROVED: "success", REJECTED: "danger", COMPLETED: "success", CANCELLED: "neutral",
};
export const StateBadge = ({ state }: { state: WorkflowState }) => <Badge tone={STATE_TONE[state]}>{state.replace("_", " ").toLowerCase()}</Badge>;

const POLICY_TONE: Record<PolicyStatus, "neutral" | "success" | "warning" | "danger"> = { pass: "success", fail: "danger", warning: "warning", unavailable: "warning", not_applicable: "neutral" };
export const PolicyBadge = ({ status }: { status: PolicyStatus }) => <Badge tone={POLICY_TONE[status]}>{status === "unavailable" ? "unavailable (not a pass)" : status.replace("_", " ")}</Badge>;

export const PRIORITY_TONE = { urgent: "danger", high: "warning", normal: "neutral", low: "neutral" } as const;

export function resourceLink(w: { resource_type: string; resource_id: string; repository_id: string | null }): string | null {
  if (!w.repository_id) return null;
  const base = `/repositories/${w.repository_id}`;
  switch (w.resource_type) {
    case "code_review": return `${base}/reviews/${w.resource_id}`;
    case "repair_patch": return `${base}/repairs/${w.resource_id}`;
    case "refactor": return `${base}/refactors/${w.resource_id}`;
    case "change_request": return `${base}/workflow/change-requests/${w.resource_id}`;
    default: return null;
  }
}

export const LIMIT_NOTE = (
  <Notice tone="info">
    Governance records are append-only and protected by database triggers, but they are not cryptographically signed, and there is no scheduler: nothing escalates or becomes overdue automatically.
  </Notice>
);

export function ageLabel(seconds: number): string {
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))} min`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)} h`;
  return `${(seconds / 86400).toFixed(1)} d`;
}
