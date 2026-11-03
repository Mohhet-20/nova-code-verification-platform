import type {
  DecisionKind, GovAuditEvent, GovEvaluation, GovEvent, GovHealth, GovMe, GovOrgMember, GovOverview, GovOwnership, GovPolicy, GovPolicyStatus, GovPolicyType, GovQueueHistory,
  GovQueueItem, GovQueueSummary, GovTeam, GovTeamMember, GovTeamWork, GovWorkflow, GovWorkflowDetail, Page, TeamRole,
} from "../types/governance";
import { qs, request } from "./api";

const post = <T>(path: string, body?: unknown) => request<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) });
const put = <T>(path: string, body: unknown) => request<T>(path, { method: "PUT", body: JSON.stringify(body) });

/** Typed client for the governance API. The server decides every authorization and workflow outcome; nothing here grants anything. */
export const govApi = {
  me: () => request<GovMe>("/governance/me"),
  overview: () => request<GovOverview>("/governance/overview"),
  health: () => request<GovHealth>("/governance/health"),

  orgMembers: (page = 1) => request<Page<GovOrgMember>>(`/org/members${qs({ page, page_size: 100 })}`),
  teams: (p: { q?: string; page?: number; includeArchived?: boolean } = {}) =>
    request<Page<GovTeam>>(`/teams${qs({ q: p.q, page: p.page ?? 1, page_size: 20, include_archived: p.includeArchived ? "true" : undefined })}`),
  team: (id: string) => request<GovTeam>(`/teams/${id}`),
  createTeam: (name: string, description: string) => post<GovTeam>("/teams", { name, description }),
  archiveTeam: (id: string) => post<GovTeam>(`/teams/${id}/archive`),
  teamMembers: (id: string) => request<Page<GovTeamMember>>(`/teams/${id}/members${qs({ page_size: 100 })}`),
  addTeamMember: (id: string, userId: string, role: TeamRole) => post<GovTeamMember>(`/teams/${id}/members`, { user_id: userId, role }),
  changeTeamMember: (id: string, userId: string, role: TeamRole) => put<GovTeamMember>(`/teams/${id}/members/${userId}`, { role }),
  removeTeamMember: (id: string, userId: string) => post<GovTeamMember>(`/teams/${id}/members/${userId}/remove`),
  ownership: (id: string) => request<Page<GovOwnership>>(`/teams/${id}/ownership${qs({ page_size: 100 })}`),
  teamWork: (id: string) => request<GovTeamWork>(`/teams/${id}/work`),

  policyTypes: () => request<GovPolicyType[]>("/governance/policy-types"),
  policies: (page = 1) => request<Page<GovPolicy>>(`/governance/policies${qs({ page, page_size: 50 })}`),
  setPolicyEnabled: (id: string, enabled: boolean) => put<GovPolicy>(`/governance/policies/${id}`, { enabled }),
  evaluations: (p: { status?: string; policyKey?: string; page?: number }) =>
    request<Page<GovEvaluation>>(`/governance/evaluations${qs({ status: p.status, policy_key: p.policyKey, page: p.page ?? 1, page_size: 20 })}`),

  workflows: (p: { state?: string; mine?: boolean; pendingForMe?: boolean; teamId?: string; page?: number }) =>
    request<Page<GovWorkflow>>(`/workflows/instances${qs({ state: p.state, mine: p.mine ? "true" : undefined, pending_for_me: p.pendingForMe ? "true" : undefined, team_id: p.teamId, page: p.page ?? 1, page_size: 20 })}`),
  workflow: (id: string) => request<GovWorkflowDetail>(`/workflows/instances/${id}`),
  workflowEvents: (id: string) => request<Page<GovEvent>>(`/workflows/instances/${id}/events${qs({ page_size: 100 })}`),
  policyStatus: (id: string) => request<GovPolicyStatus>(`/workflows/instances/${id}/policy-status`),
  createWorkflow: (resourceType: string, resourceId: string) => post<GovWorkflow>("/workflows/instances", { resource_type: resourceType, resource_id: resourceId }),
  transition: (id: string, action: "submit" | "resubmit" | "cancel" | "complete", note = "") => post<GovWorkflow>(`/workflows/instances/${id}/transitions`, { action, note }),
  decide: (id: string, decision: DecisionKind, justification: string) => post<GovWorkflow>(`/workflows/instances/${id}/decisions`, { decision, justification }),

  queue: (p: { status?: string; priority?: string; unassigned?: boolean; assignee?: string; page?: number }) =>
    request<Page<GovQueueItem>>(`/review-queue${qs({ status: p.status ?? "open", priority: p.priority, unassigned: p.unassigned ? "true" : undefined, assignee: p.assignee, page: p.page ?? 1, page_size: 20 })}`),
  queueSummary: () => request<GovQueueSummary>("/review-queue/summary"),
  claim: (id: string) => post<GovQueueItem>(`/review-queue/${id}/claim`),
  release: (id: string) => post<GovQueueItem>(`/review-queue/${id}/release`),
  queueHistory: (id: string) => request<Page<GovQueueHistory>>(`/review-queue/${id}/history`),

  audit: (p: { actionPrefix?: string; teamId?: string; page?: number }) =>
    request<Page<GovAuditEvent>>(`/governance/audit${qs({ action_prefix: p.actionPrefix, team_id: p.teamId, page: p.page ?? 1, page_size: 25 })}`),
};
