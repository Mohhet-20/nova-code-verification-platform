import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Badge, Card, EmptyState, Field, PageHeader, Pagination, TextInput } from "../components/ui";
import { govApi } from "../services/governanceApi";
import { formatDate } from "../services/format";
import { GovernanceNav, Guard, LIMIT_NOTE } from "./GovernanceShared";

export function GovernanceAuditPage() {
  const [prefix, setPrefix] = useState("");
  const [page, setPage] = useState(1);
  const q = useQuery({ queryKey: ["gov-audit", prefix, page], queryFn: () => govApi.audit({ actionPrefix: prefix || undefined, page }), placeholderData: (p) => p });
  return (
    <>
      <GovernanceNav />
      <PageHeader title="Audit history" subtitle="Read-only record of governance actions, including denied attempts. There is no way to edit or delete entries from the API or this screen." />
      <div className="mb-4 max-w-sm"><Field label="Action starts with" htmlFor="audit-prefix" hint="For example workflow., policy., team. or authz."><TextInput id="audit-prefix" value={prefix} onChange={(e) => { setPrefix(e.target.value); setPage(1); }} /></Field></div>
      <Guard query={q} what="the audit history">
        {(d) => d.items.length === 0 ? <EmptyState title="No matching events">Nothing has been recorded for this filter.</EmptyState> : (
          <Card>
            <ol className="divide-y divide-slate-100" aria-label="Audit events">
              {d.items.map((e) => (
                <li key={e.id} className="py-2 text-sm">
                  <div className="flex flex-wrap items-center justify-between gap-2"><span className="font-medium">{e.action}</span><Badge tone={e.outcome === "success" ? "success" : e.outcome === "denied" || e.outcome === "blocked" ? "warning" : "neutral"}>{e.outcome}</Badge></div>
                  <p className="text-xs text-slate-500">{e.actor_handle ?? "system"} · {e.entity_type} · {formatDate(e.at)}</p>
                </li>
              ))}
            </ol>
            <Pagination page={d.page} pages={d.pages} total={d.total} onPage={setPage} />
          </Card>
        )}
      </Guard>
      <div className="mt-6">{LIMIT_NOTE}</div>
    </>
  );
}
