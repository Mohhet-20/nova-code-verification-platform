import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Badge, Button, Card, ConfirmDialog, EmptyState, Notice, PageHeader, Pagination } from "../components/ui";
import { describeError } from "../services/api";
import { govApi } from "../services/governanceApi";
import { formatDate } from "../services/format";
import type { GovQueueItem } from "../types/governance";
import { GovernanceNav, Guard, PRIORITY_TONE, StateBadge, ageLabel, useMe } from "./GovernanceShared";

function QueueRow({ item, meId }: { item: GovQueueItem; meId?: string }) {
  const qc = useQueryClient();
  const [releasing, setReleasing] = useState(false);
  const [history, setHistory] = useState(false);
  const done = () => qc.invalidateQueries({ queryKey: ["gov-queue"] });
  const claim = useMutation({ mutationFn: () => govApi.claim(item.id), onSuccess: done });
  const release = useMutation({ mutationFn: () => govApi.release(item.id), onSuccess: async () => { setReleasing(false); await done(); } });
  const hist = useQuery({ queryKey: ["gov-queue-history", item.id], queryFn: () => govApi.queueHistory(item.id), enabled: history });
  const mine = item.assignee_user_id !== null && item.assignee_user_id === meId;
  return (
    <li className="rounded-lg border border-slate-200 bg-white p-4" data-testid="queue-item">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-medium text-slate-900">{item.resource_available ? item.resource_title : "Resource unavailable"}</p>
          <p className="mt-0.5 text-sm text-slate-600">{item.resource_type.replace("_", " ")} · {item.reason} · waiting {ageLabel(item.age_seconds)}</p>
          {!item.resource_available && <p className="mt-1 text-xs text-amber-800">The underlying record is gone. The item stays visible so the decision history is not lost; it cannot be approved.</p>}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={PRIORITY_TONE[item.priority as keyof typeof PRIORITY_TONE] ?? "neutral"}>{item.priority}</Badge>
          {item.risk_level && <Badge>{`risk ${item.risk_level}`}</Badge>}
          {item.workflow_state && <StateBadge state={item.workflow_state} />}
          <Badge tone={item.assignee ? "info" : "neutral"}>{item.assignee ? `assigned: ${item.assignee}` : "unassigned"}</Badge>
        </div>
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        {item.instance_id && <Link to={`/governance/workflows/${item.instance_id}`} className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-800 hover:bg-slate-100">Open workflow</Link>}
        {item.status === "open" && !item.assignee && <Button variant="secondary" disabled={claim.isPending} onClick={() => claim.mutate()}>Claim</Button>}
        {item.status === "open" && item.assignee && <Button variant="secondary" onClick={() => setReleasing(true)}>{mine ? "Release my claim" : "Release assignment"}</Button>}
        <Button variant="ghost" aria-expanded={history} onClick={() => setHistory((h) => !h)}>{history ? "Hide history" : "History"}</Button>
      </div>
      {claim.isError && <div className="mt-2"><Notice tone="danger">{describeError(claim.error)}</Notice></div>}
      {history && (
        <div className="mt-3 text-sm">
          {hist.isPending ? <p>Loading…</p> : hist.isError ? <Notice tone="danger">{describeError(hist.error)}</Notice> : (
            <ol className="space-y-1" aria-label="Item history">{hist.data.items.map((h) => <li key={h.id}>{h.action} · {formatDate(h.at)}{h.note ? ` — ${h.note}` : ""}</li>)}</ol>
          )}
        </div>
      )}
      {releasing && (
        <ConfirmDialog title="Release this assignment?" confirmLabel="Release" busy={release.isPending} onConfirm={() => release.mutate()} onCancel={() => setReleasing(false)} error={release.isError ? describeError(release.error) : undefined}>
          <p>The item goes back to the unassigned queue for others to claim.</p>
        </ConfirmDialog>
      )}
    </li>
  );
}

export function GovernanceQueuePage() {
  const me = useMe();
  const [status, setStatus] = useState("open");
  const [priority, setPriority] = useState("");
  const [unassigned, setUnassigned] = useState(false);
  const [mineOnly, setMineOnly] = useState(false);
  const [page, setPage] = useState(1);
  const q = useQuery({
    queryKey: ["gov-queue", status, priority, unassigned, mineOnly, page],
    queryFn: () => govApi.queue({ status, priority: priority || undefined, unassigned, assignee: mineOnly ? "me" : undefined, page }),
    placeholderData: (prev) => prev,
  });
  const reset = () => setPage(1);
  return (
    <>
      <GovernanceNav />
      <PageHeader title="Review queue" subtitle="Shared, persistent list of work waiting for independent review. Claiming an item reserves it; it does not approve anything." />
      <form className="mb-4 flex flex-wrap items-end gap-4 rounded-lg border border-slate-200 bg-white p-4" aria-label="Queue filters" onSubmit={(e) => e.preventDefault()}>
        <label className="text-sm">Status
          <select className="ml-2 rounded-md border border-slate-300 px-2 py-1" value={status} onChange={(e) => { setStatus(e.target.value); reset(); }}>
            <option value="open">Open</option><option value="done">Done</option><option value="cancelled">Cancelled</option>
          </select>
        </label>
        <label className="text-sm">Priority
          <select className="ml-2 rounded-md border border-slate-300 px-2 py-1" value={priority} onChange={(e) => { setPriority(e.target.value); reset(); }}>
            <option value="">Any</option><option value="urgent">Urgent</option><option value="high">High</option><option value="normal">Normal</option><option value="low">Low</option>
          </select>
        </label>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={unassigned} onChange={(e) => { setUnassigned(e.target.checked); reset(); }} /> Unassigned only</label>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={mineOnly} onChange={(e) => { setMineOnly(e.target.checked); reset(); }} /> Assigned to me</label>
      </form>
      <Guard query={q} what="the review queue">
        {(data) => data.items.length === 0 ? (
          <EmptyState title="Nothing in the queue">No items match these filters. Items appear here when a workflow is submitted for review.</EmptyState>
        ) : (
          <>
            <ul className="space-y-3" aria-label="Review queue items">{data.items.map((i) => <QueueRow key={i.id} item={i} meId={me.data?.user_id} />)}</ul>
            <Pagination page={data.page} pages={data.pages} total={data.total} onPage={setPage} />
          </>
        )}
      </Guard>
    </>
  );
}

export function GovernanceApprovalsPage() {
  const [page, setPage] = useState(1);
  const q = useQuery({ queryKey: ["gov-approvals", page], queryFn: () => govApi.workflows({ pendingForMe: true, page }), placeholderData: (prev) => prev });
  const mine = useQuery({ queryKey: ["gov-approvals-mine"], queryFn: () => govApi.workflows({ mine: true }) });
  return (
    <>
      <GovernanceNav />
      <PageHeader title="My approvals" subtitle="Workflows currently waiting on a decision you are eligible to make. Eligibility is decided by the server." />
      <Guard query={q} what="approvals">
        {(data) => data.items.length === 0 ? <EmptyState title="Nothing is waiting on you">When a workflow needs your decision it will be listed here.</EmptyState> : (
          <>
            <ul className="space-y-3" aria-label="Pending approvals">
              {data.items.map((w) => (
                <li key={w.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slate-200 bg-white p-4">
                  <div>
                    <Link to={`/governance/workflows/${w.id}`} className="font-medium text-sky-900 underline">{w.title ?? `${w.resource_type.replace("_", " ")} approval`}</Link>
                    <p className="text-sm text-slate-600">Requested by {w.requested_by_handle ?? "unknown"} · round {w.round} · priority {w.priority}</p>
                  </div>
                  <StateBadge state={w.state} />
                </li>
              ))}
            </ul>
            <Pagination page={data.page} pages={data.pages} total={data.total} onPage={setPage} />
          </>
        )}
      </Guard>
      <div className="mt-8">
        <Card title="Requests I started">
          {mine.isPending ? <p className="text-sm text-slate-600">Loading…</p> : mine.isError ? <Notice tone="danger">{describeError(mine.error)}</Notice> : mine.data.items.length === 0 ? <p className="text-sm text-slate-600">You have not started any workflows.</p> : (
            <ul className="space-y-2" aria-label="My requests">
              {mine.data.items.map((w) => <li key={w.id} className="flex items-center justify-between text-sm"><Link className="text-sky-900 underline" to={`/governance/workflows/${w.id}`}>{w.title ?? w.resource_type.replace("_", " ")}</Link><StateBadge state={w.state} /></li>)}
            </ul>
          )}
        </Card>
      </div>
    </>
  );
}
