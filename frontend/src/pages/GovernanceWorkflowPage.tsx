import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Badge, Button, Card, ConfirmDialog, EmptyState, Field, Notice, PageHeader, TextArea } from "../components/ui";
import { ApiError, describeError } from "../services/api";
import { govApi } from "../services/governanceApi";
import { formatDate } from "../services/format";
import type { DecisionKind, GovWorkflowDetail } from "../types/governance";
import { GovernanceNav, Guard, PolicyBadge, StateBadge, can, resourceLink, useMe } from "./GovernanceShared";

const DECISIONS: { value: DecisionKind; label: string; help: string; needsReason: boolean }[] = [
  { value: "approve", label: "Approve", help: "Counts toward the stage quorum. Final approval is still subject to policy checks.", needsReason: false },
  { value: "request_changes", label: "Request changes", help: "Returns the work to the requester, who must resubmit.", needsReason: true },
  { value: "reject", label: "Reject", help: "Final: a rejected workflow cannot be resubmitted.", needsReason: true },
  { value: "abstain", label: "Abstain", help: "Records that you reviewed it without approving or blocking.", needsReason: false },
];

function blockedDetails(error: unknown): string[] | null {
  if (error instanceof ApiError && error.status === 409 && typeof error.details === "object" && error.details && (error.details as { code?: string }).code === "policy_blocked") {
    return ((error.details as { blockers?: string[] }).blockers ?? []).map(String);
  }
  return null;
}

function DecisionPanel({ wf }: { wf: GovWorkflowDetail }) {
  const qc = useQueryClient();
  const me = useMe();
  const [decision, setDecision] = useState<DecisionKind>("approve");
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState(false);
  const spec = DECISIONS.find((d) => d.value === decision)!;
  const mutation = useMutation({
    mutationFn: () => govApi.decide(wf.id, decision, reason.trim()),
    onSuccess: async () => {
      setConfirming(false);
      setReason("");
      await qc.invalidateQueries({ queryKey: ["gov-wf", wf.id] });
      await qc.invalidateQueries({ queryKey: ["gov-queue"] });
      await qc.invalidateQueries({ queryKey: ["gov-approvals"] });
    },
  });
  const mine = me.data && wf.decisions.some((d) => d.user_id === me.data.user_id && d.round === wf.round && d.stage_index === wf.stage_index);
  if (me.data && me.data.user_id === wf.requested_by) {
    return <Notice tone="info">You requested this review. Independent reviewers decide; you cannot approve your own request.</Notice>;
  }
  const blockers = blockedDetails(mutation.error);
  const invalid = spec.needsReason && reason.trim().length === 0;
  return (
    <Card title="Record your decision">
      {mine && <Notice tone="info">You have already recorded a decision for this stage and round.</Notice>}
      <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); if (!invalid) setConfirming(true); }}>
        <fieldset>
          <legend className="mb-1 text-sm font-medium text-slate-800">Decision</legend>
          <div className="flex flex-wrap gap-4">
            {DECISIONS.map((d) => (
              <label key={d.value} className="flex items-center gap-2 text-sm">
                <input type="radio" name="decision" value={d.value} checked={decision === d.value} onChange={() => { setDecision(d.value); mutation.reset(); }} />
                {d.label}
              </label>
            ))}
          </div>
          <p className="mt-1 text-xs text-slate-500">{spec.help}</p>
        </fieldset>
        <Field label={spec.needsReason ? "Justification (required)" : "Justification (optional)"} htmlFor="justification">
          <TextArea id="justification" rows={3} maxLength={2000} value={reason} onChange={(e) => setReason(e.target.value)} aria-required={spec.needsReason} />
        </Field>
        <Button type="submit" disabled={invalid || !!mine}>Review decision…</Button>
      </form>
      {mutation.isError && !confirming && <div className="mt-3"><Notice tone="danger">{describeError(mutation.error)}</Notice></div>}
      {confirming && (
        <ConfirmDialog
          title={`Confirm: ${spec.label.toLowerCase()}`}
          confirmLabel={`Confirm ${spec.label.toLowerCase()}`}
          tone={decision === "reject" ? "danger" : "primary"}
          busy={mutation.isPending}
          onConfirm={() => mutation.mutate()}
          onCancel={() => { setConfirming(false); mutation.reset(); }}
          error={mutation.isError ? (blockers ? <>Blocked by policy: {blockers.join(", ") || "see policy status"}. Nothing was approved.</> : describeError(mutation.error)) : undefined}
        >
          <p>This is recorded permanently in the audit history under your name.</p>
          <p>{spec.help}</p>
          {reason.trim() && <p className="rounded bg-slate-50 p-2 text-xs">“{reason.trim()}”</p>}
        </ConfirmDialog>
      )}
    </Card>
  );
}

function Transitions({ wf }: { wf: GovWorkflowDetail }) {
  const qc = useQueryClient();
  const me = useMe();
  const [pending, setPending] = useState<"submit" | "resubmit" | "cancel" | "complete" | null>(null);
  const m = useMutation({
    mutationFn: (a: "submit" | "resubmit" | "cancel" | "complete") => govApi.transition(wf.id, a),
    onSuccess: async () => { setPending(null); await qc.invalidateQueries({ queryKey: ["gov-wf", wf.id] }); await qc.invalidateQueries({ queryKey: ["gov-queue"] }); },
  });
  const requester = me.data?.user_id === wf.requested_by;
  const lead = can(me.data, "workflow.complete");
  const actions: { a: "submit" | "resubmit" | "cancel" | "complete"; label: string; show: boolean; danger?: boolean; text: string }[] = [
    { a: "submit", label: "Submit for review", show: wf.state === "DRAFT" && (requester || lead), text: "The workflow will be queued for independent reviewers." },
    { a: "resubmit", label: "Resubmit", show: wf.state === "CHANGES_REQUESTED" && (requester || lead), text: "Starts a new review round; earlier decisions stay in the history but no longer count." },
    { a: "complete", label: "Mark completed", show: wf.state === "APPROVED" && (requester || lead), text: "Closes the approved workflow. Policies are re-checked first." },
    { a: "cancel", label: "Cancel workflow", danger: true, show: ["DRAFT", "SUBMITTED", "IN_REVIEW", "CHANGES_REQUESTED", "APPROVED"].includes(wf.state) && (requester || can(me.data, "workflow.cancel")), text: "Stops this workflow permanently." },
  ];
  const visible = actions.filter((x) => x.show);
  if (visible.length === 0) return null;
  const cur = actions.find((x) => x.a === pending);
  return (
    <div className="flex flex-wrap gap-2">
      {visible.map((x) => <Button key={x.a} variant={x.danger ? "danger" : "secondary"} onClick={() => { m.reset(); setPending(x.a); }}>{x.label}</Button>)}
      {cur && (
        <ConfirmDialog title={cur.label} confirmLabel={cur.label} tone={cur.danger ? "danger" : "primary"} busy={m.isPending} onConfirm={() => m.mutate(cur.a)} onCancel={() => setPending(null)}
          error={m.isError ? describeError(m.error) : undefined}>
          <p>{cur.text}</p>
        </ConfirmDialog>
      )}
    </div>
  );
}

export function GovernanceWorkflowPage() {
  const { workflowId = "" } = useParams();
  const q = useQuery({ queryKey: ["gov-wf", workflowId], queryFn: () => govApi.workflow(workflowId) });
  const events = useQuery({ queryKey: ["gov-wf-events", workflowId, q.data?.updated_at], queryFn: () => govApi.workflowEvents(workflowId), enabled: q.isSuccess });
  const policy = useQuery({ queryKey: ["gov-wf-policy", workflowId, q.data?.updated_at], queryFn: () => govApi.policyStatus(workflowId), enabled: q.isSuccess });
  return (
    <>
      <GovernanceNav />
      <Guard query={q} what="this workflow">
        {(wf) => {
          const link = resourceLink(wf);
          return (
            <div className="space-y-5">
              <PageHeader title={wf.title ?? `${wf.resource_type.replace("_", " ")} approval`} subtitle={<>Round {wf.round} · {wf.definition_key} v{wf.definition_version} · requested by {wf.requested_by_handle ?? "unknown"}</>} actions={<StateBadge state={wf.state} />} />
              {!wf.resource_available && <Notice tone="warning">The reviewed resource is no longer available. Its evidence cannot be evaluated, so approval stays blocked until it is restored.</Notice>}
              <Card title="Subject">
                <p className="text-sm text-slate-700">{wf.resource_type.replace("_", " ")} · priority {wf.priority}{wf.risk_level ? ` · risk ${wf.risk_level}` : ""}
                  {link && wf.resource_available ? <> · <Link className="text-sky-800 underline" to={link}>Open the {wf.resource_type.replace("_", " ")}</Link></> : null}</p>
                <ul className="mt-3 list-disc pl-5 text-sm text-slate-700" aria-label="Outstanding actions">{wf.outstanding_actions.map((o) => <li key={o}>{o}</li>)}</ul>
              </Card>
              <Transitions wf={wf} />
              <Card title="Approval stages">
                <ol className="space-y-2">
                  {wf.stages.map((s) => (
                    <li key={s.index} className="flex flex-wrap items-center justify-between gap-2 rounded border border-slate-200 px-3 py-2 text-sm">
                      <span><strong>{s.name}</strong> — {s.approvals}/{s.min_approvals} approval(s) · {s.approver_roles.concat(s.approver_team_roles.map((r) => `team ${r}`)).join(", ")}{s.independent ? " · independent" : ""}</span>
                      <Badge tone={s.satisfied ? "success" : s.open ? "info" : "neutral"}>{s.satisfied ? "satisfied" : s.open ? "open" : "waiting"}</Badge>
                    </li>
                  ))}
                </ol>
              </Card>
              {wf.state === "IN_REVIEW" && <DecisionPanel wf={wf} />}
              <Card title="Policy checks" actions={policy.data?.blocked ? <Badge tone="danger">would block</Badge> : undefined}>
                {policy.isPending ? <p className="text-sm text-slate-600">Evaluating…</p> : policy.isError ? <Notice tone="warning">Policy status is unavailable: {describeError(policy.error)}. Unavailable is never treated as a pass.</Notice> : policy.data.results.length === 0 ? <p className="text-sm text-slate-600">No policies apply to this workflow.</p> : (
                  <ul className="space-y-2" aria-label="Policy results">
                    {policy.data.results.map((r) => (
                      <li key={r.policy.key} className="rounded border border-slate-200 px-3 py-2 text-sm">
                        <div className="flex items-center justify-between gap-2"><span className="font-medium">{r.policy.name} <span className="text-xs text-slate-500">v{r.policy.version} · {r.policy.severity}</span></span><PolicyBadge status={r.status} /></div>
                        <p className="mt-1 text-slate-600">{r.explanation}</p>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
              <Card title="Timeline">
                {events.isPending ? <p className="text-sm text-slate-600">Loading history…</p> : events.isError ? <Notice tone="warning">{describeError(events.error)}</Notice> : events.data.items.length === 0 ? <EmptyState title="No events yet" /> : (
                  <ol className="space-y-2" aria-label="Workflow timeline">
                    {events.data.items.map((e) => {
                      return (
                        <li key={e.id} className="border-l-2 border-sky-200 pl-3 text-sm">
                          <p className="font-medium text-slate-800">{e.event_type === "decision_recorded" ? `Decision: ${String(e.data.decision ?? "")}` : e.to_state ? `${e.from_state ?? "—"} → ${e.to_state}` : e.event_type.replace("_", " ")}</p>
                          <p className="text-xs text-slate-500">{e.actor_handle ?? "system"} · {formatDate(e.at)}</p>
                          {typeof e.data.justification === "string" && e.data.justification && <p className="mt-0.5 text-slate-600">“{e.data.justification}”</p>}
                        </li>
                      );
                    })}
                  </ol>
                )}
              </Card>
            </div>
          );
        }}
      </Guard>
    </>
  );
}
