import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Badge, Button, Card, ConfirmDialog, EmptyState, PageHeader, Pagination } from "../components/ui";
import { describeError } from "../services/api";
import { govApi } from "../services/governanceApi";
import { formatDate } from "../services/format";
import type { GovPolicy } from "../types/governance";
import { GovernanceNav, Guard, PolicyBadge, can, useMe } from "./GovernanceShared";

function PolicyRow({ policy, canManage }: { policy: GovPolicy; canManage: boolean }) {
  const qc = useQueryClient();
  const [confirm, setConfirm] = useState(false);
  const toggle = useMutation({ mutationFn: () => govApi.setPolicyEnabled(policy.id, !policy.enabled), onSuccess: async () => { setConfirm(false); await qc.invalidateQueries({ queryKey: ["gov-policies"] }); } });
  const [open, setOpen] = useState(false);
  return (
    <li className="rounded-lg border border-slate-200 bg-white p-4" data-testid="policy-item">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div><p className="font-medium">{policy.name} <span className="text-xs text-slate-500">v{policy.version} · {policy.key}</span></p><p className="text-sm text-slate-600">{policy.description || policy.policy_type}</p></div>
        <div className="flex items-center gap-2">
          <Badge tone={policy.severity === "blocking" ? "danger" : policy.severity === "warning" ? "warning" : "neutral"}>{policy.severity}</Badge>
          <Badge tone={policy.enabled ? "success" : "neutral"}>{policy.enabled ? "enabled" : "disabled"}</Badge>
        </div>
      </div>
      <p className="mt-1 text-xs text-slate-500">{policy.policy_type.replace(/_/g, " ")} · applies to {policy.resource_types.length ? policy.resource_types.join(", ") : "all governed resources"}</p>
      <div className="mt-2 flex gap-2">
        <Button variant="ghost" aria-expanded={open} onClick={() => setOpen((o) => !o)}>{open ? "Hide configuration" : "Show configuration"}</Button>
        {canManage && <Button variant="secondary" onClick={() => { toggle.reset(); setConfirm(true); }}>{policy.enabled ? "Disable" : "Enable"}</Button>}
      </div>
      {open && <pre className="mt-2 overflow-x-auto rounded bg-slate-50 p-2 text-xs" aria-label="Policy configuration">{JSON.stringify(policy.config, null, 2)}</pre>}
      {confirm && (
        <ConfirmDialog title={`${policy.enabled ? "Disable" : "Enable"} “${policy.name}”?`} confirmLabel={policy.enabled ? "Disable policy" : "Enable policy"} tone={policy.enabled ? "danger" : "primary"} busy={toggle.isPending} onConfirm={() => toggle.mutate()} onCancel={() => setConfirm(false)} error={toggle.isError ? describeError(toggle.error) : undefined}>
          <p>{policy.enabled ? "A disabled policy no longer gates approvals. Workflows that list it as required will be blocked until it is enabled again." : "Enabling takes effect for the next evaluation."} The change creates a new policy version and an audit record.</p>
        </ConfirmDialog>
      )}
    </li>
  );
}

export function GovernancePoliciesPage() {
  const me = useMe();
  const [tab, setTab] = useState<"policies" | "evaluations">("policies");
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState("");
  const pol = useQuery({ queryKey: ["gov-policies", page], queryFn: () => govApi.policies(page), placeholderData: (p) => p, enabled: tab === "policies" });
  const ev = useQuery({ queryKey: ["gov-evals", status, page], queryFn: () => govApi.evaluations({ status: status || undefined, page }), placeholderData: (p) => p, enabled: tab === "evaluations" });
  const manage = can(me.data, "policy.manage");
  return (
    <>
      <GovernanceNav />
      <PageHeader title="Policies" subtitle="Declarative rules evaluated against recorded evidence. A policy never executes code, and missing evidence is reported as unavailable, never as a pass." />
      <div role="tablist" aria-label="Policy views" className="mb-4 flex gap-2">
        {(["policies", "evaluations"] as const).map((t) => (
          <button key={t} role="tab" type="button" aria-selected={tab === t} onClick={() => { setTab(t); setPage(1); }} className={`rounded-md px-3 py-1.5 text-sm font-medium ${tab === t ? "bg-sky-100 text-sky-900" : "text-slate-600 hover:bg-slate-100"}`}>{t === "policies" ? "Policies" : "Evaluation history"}</button>
        ))}
      </div>
      {tab === "policies" ? (
        <Guard query={pol} what="policies">
          {(d) => d.items.length === 0 ? <EmptyState title="No policies configured">Without policies, workflows have no automated gate. An organization administrator can install the defaults.</EmptyState> : (
            <><ul className="space-y-3" aria-label="Policies">{d.items.map((p) => <PolicyRow key={p.id} policy={p} canManage={manage} />)}</ul><Pagination page={d.page} pages={d.pages} total={d.total} onPage={setPage} /></>
          )}
        </Guard>
      ) : (
        <>
          <label className="mb-3 block text-sm">Result
            <select className="ml-2 rounded-md border border-slate-300 px-2 py-1" value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }}>
              <option value="">Any</option><option value="fail">Fail</option><option value="unavailable">Unavailable</option><option value="warning">Warning</option><option value="pass">Pass</option>
            </select>
          </label>
          <Guard query={ev} what="evaluations">
            {(d) => d.items.length === 0 ? <EmptyState title="No evaluations yet">Evaluations are recorded when a workflow is approved, completed or blocked, or when evaluated on demand.</EmptyState> : (
              <Card>
                <ul className="divide-y divide-slate-100" aria-label="Evaluations">
                  {d.items.map((e) => (
                    <li key={e.id} className="py-2 text-sm">
                      <div className="flex flex-wrap items-center justify-between gap-2"><span><strong>{e.policy_key}</strong> v{e.policy_version} · {e.resource_type.replace("_", " ")} · {e.trigger}</span><PolicyBadge status={e.status} /></div>
                      <p className="text-slate-600">{e.result.explanation}</p><p className="text-xs text-slate-500">{formatDate(e.evaluated_at)}</p>
                    </li>
                  ))}
                </ul>
                <Pagination page={d.page} pages={d.pages} total={d.total} onPage={setPage} />
              </Card>
            )}
          </Guard>
        </>
      )}
    </>
  );
}
