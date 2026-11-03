import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Badge, Card, EmptyState, Notice, PageHeader, Stat } from "../components/ui";
import { describeError } from "../services/api";
import { govApi } from "../services/governanceApi";
import { GovernanceNav, Guard, LIMIT_NOTE, StateBadge, useMe } from "./GovernanceShared";

function Health() {
  const q = useQuery({ queryKey: ["gov-health"], queryFn: govApi.health, retry: false });
  if (q.isPending) return <p className="text-sm text-slate-600">Checking governance configuration…</p>;
  if (q.isError) return <p className="text-sm text-slate-600">Governance health is visible to organization administrators and auditors only.</p>;
  return (
    <Card title="Governance configuration" actions={<Badge tone={q.data.healthy ? "success" : "danger"}>{q.data.healthy ? "healthy" : "needs attention"}</Badge>}>
      <ul className="space-y-1 text-sm" aria-label="Health checks">
        {q.data.checks.map((c) => <li key={c.name}><span aria-hidden>{c.ok ? "✓" : "✗"}</span> <span className="sr-only">{c.ok ? "ok" : "failing"}: </span><strong>{c.name.replace(/_/g, " ")}</strong> — {c.detail}</li>)}
      </ul>
      <p className="mt-3 text-xs text-slate-500">{q.data.limits}</p>
    </Card>
  );
}

export function GovernanceHomePage() {
  const me = useMe();
  const q = useQuery({ queryKey: ["gov-overview"], queryFn: govApi.overview });
  return (
    <>
      <GovernanceNav />
      <PageHeader title="Governance" subtitle={me.data ? <>Signed in as <strong>{me.data.display_name || me.data.handle}</strong> · organization role <strong>{me.data.org_role}</strong>{me.data.org_wide_visibility ? " · organization-wide visibility" : " · scoped to your teams"}</> : "Teams, approvals and policies."} />
      <Guard query={q} what="governance">
        {(o) => (
          <div className="space-y-5">
            <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Stat label="Waiting on you" value={o.pending_my_approval} />
              <Stat label="Open in queue" value={o.queue.by_status.open ?? 0} hint={`${o.queue.unassigned} unassigned`} />
              <Stat label="My open requests" value={o.my_open_requests} />
              <Stat label="Teams" value={o.teams} />
            </dl>
            <Card title="Waiting for your decision">
              {o.pending_preview.length === 0 ? <EmptyState title="Nothing is waiting on you" /> : (
                <ul className="space-y-2">{o.pending_preview.map((w) => <li key={w.id} className="flex items-center justify-between text-sm"><Link className="text-sky-900 underline" to={`/governance/workflows/${w.id}`}>{w.title ?? w.resource_type.replace("_", " ")}</Link><StateBadge state={w.state} /></li>)}</ul>
              )}
            </Card>
            <Health />
            {LIMIT_NOTE}
          </div>
        )}
      </Guard>
      {me.isError && <div className="mt-4"><Notice tone="danger">{describeError(me.error)}</Notice></div>}
    </>
  );
}
