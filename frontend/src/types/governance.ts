import type { Page } from "./api";

export type { Page };
export type OrgRole = "owner" | "maintainer" | "engineer" | "reviewer" | "auditor" | "viewer";
export type TeamRole = "admin" | "member" | "reviewer";
export type WorkflowState = "DRAFT" | "SUBMITTED" | "IN_REVIEW" | "CHANGES_REQUESTED" | "APPROVED" | "REJECTED" | "COMPLETED" | "CANCELLED";
export type DecisionKind = "approve" | "reject" | "request_changes" | "abstain";
export type PolicyStatus = "pass" | "fail" | "warning" | "unavailable" | "not_applicable";

export interface GovMe {
  user_id: string; handle: string; display_name: string; org_role: OrgRole; org_wide_visibility: boolean; org_permissions: string[];
  teams: { team_id: string; role: TeamRole; permissions: string[] }[]; org_roles: string[]; auth_kind: string;
}
export interface GovTeam { id: string; name: string; description: string; status: "active" | "archived"; created_at: string; updated_at: string; archived_at: string | null; member_count: number; my_role: TeamRole | null }
export interface GovTeamMember { user_id: string; handle: string; display_name: string; role: TeamRole; status: string; joined_at: string; removed_at: string | null; added_by: string | null }
export interface GovOrgMember { user_id: string; handle: string; display_name: string; role: OrgRole; status: string; joined_at: string; removed_at: string | null }
export interface GovOwnership { repository_id: string; repository_name: string; team_id: string; team_name: string; accountable_user_id: string | null; accountable_handle: string | null; assigned_at: string; assigned_by: string | null }
export interface GovTeamWork { team_id: string; owned_repositories: number; workflows_by_state: Record<string, number>; queue_by_status: Record<string, number>; queue_unassigned: number; outstanding_approvals: number }

export interface GovStage { index: number; name: string; min_approvals: number; approvals: number; independent: boolean; approver_roles: string[]; approver_team_roles: string[]; open: boolean; satisfied: boolean }
export interface GovWorkflow {
  id: string; state: WorkflowState; round: number; stage_index: number; resource_type: string; resource_id: string; repository_id: string | null; team_id: string | null; title: string | null;
  priority: string; risk_level: string | null; requested_by: string | null; requested_by_handle: string | null; definition_key: string; definition_version: number; mode: string;
  stages: GovStage[]; outstanding_actions: string[]; submitted_at: string | null; completed_at: string | null; created_at: string; updated_at: string;
}
export interface GovDecision { id: string; round: number; stage_index: number; user_id: string | null; user_handle: string | null; decision: DecisionKind; justification: string; created_at: string }
export interface GovEvalSummary { id: string; policy_key: string; policy_version: number; status: PolicyStatus; blocking: boolean; trigger: string; explanation: string | null; evaluated_at: string }
export interface GovWorkflowDetail extends GovWorkflow { decisions: GovDecision[]; policy_evaluations: GovEvalSummary[]; resource_available: boolean }
export interface GovEvent { id: string; event_type: string; from_state: string | null; to_state: string | null; actor_user_id: string | null; actor_handle: string | null; data: Record<string, unknown>; at: string }

export interface GovPolicyResult {
  policy: { id: string | null; key: string; name: string; version: number; type: string; severity: string }; status: PolicyStatus; raw_status: PolicyStatus; blocking: boolean;
  rules: { rule_id: string; status: string; detail: string }[]; explanation: string; unmet: string[]; evaluated_at: string;
}
export interface GovPolicyStatus { available: boolean; results: GovPolicyResult[]; blocked: boolean; blocking_policies: string[]; blockers: string[] }
export interface GovPolicy {
  id: string; key: string; name: string; description: string; policy_type: string; resource_types: string[]; team_id: string | null; repository_id: string | null; severity: string; enabled: boolean;
  config: Record<string, unknown>; version: number; created_at: string; updated_at: string;
}
export interface GovPolicyType { type: string; description: string; config_schema: Record<string, unknown> }
export interface GovEvaluation {
  id: string; policy_key: string; policy_version: number; policy_type: string; severity: string; instance_id: string | null; resource_type: string; resource_id: string; status: PolicyStatus;
  blocking: boolean; trigger: string; result: { explanation: string; unmet: string[] }; evaluated_at: string;
}

export interface GovQueueItem {
  id: string; instance_id: string | null; resource_type: string; resource_id: string; resource_available: boolean; resource_title: string; repository_id: string | null; team_id: string | null;
  reason: string; priority: string; risk_level: string | null; status: "open" | "done" | "cancelled"; assignee_user_id: string | null; assignee: string | null; created_at: string;
  closed_at: string | null; age_seconds: number; workflow_state: WorkflowState | null; outstanding_actions: string[]; overdue: boolean | null;
}
export interface GovQueueHistory { id: string; action: string; actor_user_id: string | null; note: string; at: string }
export interface GovQueueSummary { by_status: Record<string, number>; assigned_to_me: number; unassigned: number }
export interface GovAuditEvent { id: string; action: string; entity_type: string; entity_id: string; actor_user_id: string | null; actor_handle: string | null; outcome: string; details: Record<string, unknown>; at: string }
export interface GovOverview { teams: number; pending_my_approval: number; pending_preview: GovWorkflow[]; queue: GovQueueSummary; my_open_requests: number; org_wide: boolean; role: string }
export interface GovHealth { healthy: boolean; checks: { name: string; ok: boolean; detail: string }[]; scheduler: string; limits: string }
