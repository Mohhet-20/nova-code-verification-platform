import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Badge, Button, Card, EmptyState, ErrorState, Notice, PageHeader, Pagination, Spinner, Stat } from "../components/ui";
import { api } from "../services/api";
import { formatDate } from "../services/format";
import type { OpsAlertEval, OpsIncident } from "../types/api";
import { MutationError } from "./RepairsPage";
import { DiagnosticsPanel, InvestigationPanel, SloPanel, stateTone } from "./OperationsPanels";

const TABS = [["overview", "System overview"], ["workflows", "Workflow performance"], ["incidents", "Incidents & alerts"], ["slos", "SLOs"], ["investigate", "Traces & logs"]] as const;
type Tab = (typeof TABS)[number][0];
const STALE_MS = 120_000;

const fmtUptime = (s: number) => (s >= 3600 ? `${(s / 3600).toFixed(1)} h` : `${Math.round(s / 60)} min`);
const SEV_TONE = { critical: "danger", warning: "warning", info: "neutral" } as const;
const STATUS_TONE = { open: "danger", reopened: "danger", acknowledged: "warning", investigating: "info", resolved: "success" } as const;

function Overview() {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 15_000); return () => clearInterval(t); }, []);
  const ov = useQuery({ queryKey: ["ops-overview"], queryFn: api.opsOverview, refetchInterval: 30_000 });
  const health = useQuery({ queryKey: ["ops-health"], queryFn: () => api.opsHealth(false) });
  const signals = useQuery({ queryKey: ["ops-signals"], queryFn: api.opsSignals });
  if (ov.isPending) return <Spinner label="Loading operational overview…" />;
  if (ov.isError && !ov.data) return <ErrorState error={ov.error} onRetry={() => void ov.refetch()} />;
  const d = ov.data!;
  const stale = ov.isError || now - ov.dataUpdatedAt > STALE_MS;
  return (
    <div className="space-y-4" data-testid="ops-overview">
      {stale && <Notice tone="warning">These figures may be stale{ov.isError ? ": the last refresh failed." : "."}</Notice>}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Health" value={<Badge tone={stateTone(d.health.status)}>{d.health.status}</Badge>} hint={d.health.critical_unavailable.length ? `Unavailable: ${d.health.critical_unavailable.join(", ")}` : d.health.degraded.length ? `Degraded: ${d.health.degraded.join(", ")}` : undefined} />
        <Stat label="Requests (1 h)" value={d.requests_last_hour.total} hint={`${d.requests_last_hour.errors_5xx} server errors`} />
        <Stat label="p95 latency" value={d.requests_last_hour.p95_seconds == null ? "no data" : `≤ ${d.requests_last_hour.p95_seconds}s`} hint="Upper histogram bucket bound" />
        <Stat label="Open incidents" value={d.open_incidents} />
        <Stat label="Jobs (1 h)" value={d.jobs_last_hour.total} hint={`${d.jobs_last_hour.failed} failed · ${d.jobs_last_hour.in_flight} running`} />
        <Stat label="Uptime" value={fmtUptime(d.resources.uptime_seconds)} hint={`Telemetry since ${formatDate(d.observed_since)}`} />
        <Stat label="Memory" value={d.resources.memory_bytes == null ? "not measurable" : `${(d.resources.memory_bytes / 1048576).toFixed(0)} MB`} hint={`${d.resources.threads} threads`} />
        <Stat label="Process CPU" value={`${d.resources.cpu_seconds.toFixed(1)} s`} />
      </div>
      <Card title="Dependencies">
        {health.isPending ? <Spinner /> : health.isError ? <ErrorState error={health.error} /> : (
          <table className="w-full text-sm"><thead className="text-left text-xs text-slate-500"><tr><th>Dependency</th><th>State</th><th>Latency</th><th>Detail</th></tr></thead>
            <tbody>{health.data.dependencies.map((x) => (
              <tr key={x.name} className="border-t border-slate-100"><td className="py-1.5">{x.name}{x.critical && <span className="ml-1 text-xs text-slate-500">(critical)</span>}</td>
                <td><Badge tone={stateTone(x.state)}>{x.state}</Badge></td><td>{x.latency_ms != null ? `${x.latency_ms} ms` : x.probed === false ? "not probed" : "–"}</td>
                <td className="text-xs text-slate-600">{x.error_class ?? x.detail ?? ""}</td></tr>))}</tbody></table>
        )}
      </Card>
      <Card title="Monitoring and notifications">
        <p className="text-sm">{d.periodic_evaluation.enabled ? `Alerts are evaluated periodically${d.periodic_evaluation.last_run ? ` (last ${formatDate(d.periodic_evaluation.last_run)})` : ""}.` : "Alerts are evaluated only when you ask. NOVA is not continuously monitoring."}</p>
        <ul className="mt-2 list-disc pl-5 text-sm">{d.notifications.channels.map((c) => <li key={c.channel}>{c.channel}: {c.implemented === false ? "not implemented" : c.enabled ? "enabled" : "disabled"}</li>)}</ul>
      </Card>
      <Card title="Signals">
        {signals.data ? <table className="w-full text-sm"><thead className="text-left text-xs text-slate-500"><tr><th>Signal</th><th>Status</th><th>Exported</th><th>Limits</th></tr></thead>
          <tbody>{signals.data.signals.map((s) => <tr key={s.signal} className="border-t border-slate-100 align-top"><td className="py-1.5">{s.signal}</td><td>{s.status.replace("_", " ")}</td><td className="text-xs">{s.exported}</td><td className="text-xs text-slate-600">{s.limits}</td></tr>)}</tbody></table> : <Spinner />}
      </Card>
      <Notice>{d.limitations.join(" ")}</Notice>
    </div>
  );
}

function Workflows() {
  const ov = useQuery({ queryKey: ["ops-overview"], queryFn: api.opsOverview });
  if (ov.isPending) return <Spinner />;
  if (ov.isError) return <ErrorState error={ov.error} />;
  const rows = Object.entries(ov.data.workflows_last_hour);
  if (!rows.length) return <EmptyState title="No workflow executions recorded">Metrics start at process start and only count real executions (indexing, analysis, tests, verification, security scans, CI/CD). Nothing is simulated.</EmptyState>;
  return (
    <Card title="Workflow outcomes (last hour, since process start)">
      <table className="w-full text-sm" data-testid="workflow-table"><thead className="text-left text-xs text-slate-500"><tr><th>Workflow</th><th>Succeeded</th><th>Failed</th><th>Other</th></tr></thead>
        <tbody>{rows.map(([wf, o]) => {
          const other = Object.entries(o).filter(([k]) => k !== "succeeded" && k !== "failed").reduce((a, [, v]) => a + v, 0);
          return <tr key={wf} className="border-t border-slate-100"><td className="py-1.5">{wf.replace(/_/g, " ")}</td><td>{o.succeeded ?? 0}</td><td className={(o.failed ?? 0) > 0 ? "font-medium text-red-700" : ""}>{o.failed ?? 0}</td><td>{other}</td></tr>;
        })}</tbody></table>
      <p className="mt-2 text-xs text-slate-500">Outcomes describe whether the workflow executed correctly, not the verdict on the code under test.</p>
    </Card>
  );
}

function IncidentRow({ inc }: { inc: OpsIncident }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const detail = useQuery({ queryKey: ["ops-incident", inc.id, inc.status, inc.occurrences], queryFn: () => api.opsIncident(inc.id), enabled: open });
  const act = useMutation({
    mutationFn: (a: "acknowledge" | "investigate" | "resolve" | "reopen") => api.opsIncidentAction(inc.id, a, note || undefined),
    onSuccess: () => { setNote(""); void qc.invalidateQueries({ queryKey: ["ops-incidents"] }); void qc.invalidateQueries({ queryKey: ["ops-overview"] }); },
  });
  const next: Record<OpsIncident["status"], ("acknowledge" | "investigate" | "resolve" | "reopen")[]> = {
    open: ["acknowledge", "investigate", "resolve"], reopened: ["acknowledge", "investigate", "resolve"], acknowledged: ["investigate", "resolve"], investigating: ["resolve"], resolved: ["reopen"],
  };
  return (
    <li className="rounded border border-slate-200 p-3" data-testid="incident-row">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={SEV_TONE[inc.severity]}>{inc.severity}</Badge><Badge tone={STATUS_TONE[inc.status]}>{inc.status}</Badge>
        <button className="text-left text-sm font-medium underline-offset-2 hover:underline" aria-expanded={open} onClick={() => setOpen(!open)}>{inc.title}</button>
        <span className="text-xs text-slate-500">{inc.component} · seen {inc.occurrences}× · last {formatDate(inc.last_seen_at)}</span>
      </div>
      <p className="mt-1 text-sm text-slate-700">{inc.summary}</p>
      {open && (
        <div className="mt-3 space-y-2 text-sm">
          {detail.isPending ? <Spinner /> : detail.isError ? <ErrorState error={detail.error} /> : (
            <>
              <h4 className="font-medium">History</h4>
              <ol className="list-decimal pl-5">{detail.data.history.map((h) => <li key={h.id}>{h.event_type.replace("incident_", "")} · {formatDate(h.occurred_at)}{h.note ? ` — ${h.note}` : ""}</li>)}</ol>
              {detail.data.deliveries.length > 0 && <><h4 className="font-medium">Notification attempts</h4><ul className="list-disc pl-5">{detail.data.deliveries.map((d) => <li key={d.id}>{d.channel} → {d.destination}: {d.status}{d.error_class ? ` (${d.error_class})` : ""}, attempt {d.attempt}</li>)}</ul></>}
              <details><summary className="cursor-pointer">Evidence</summary><pre className="mt-1 overflow-x-auto rounded bg-slate-50 p-2 text-xs">{JSON.stringify(detail.data.evidence, null, 2)}</pre></details>
            </>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <input aria-label="Note" placeholder="Note (required to resolve)" value={note} onChange={(e) => setNote(e.target.value)} className="min-w-[14rem] rounded border border-slate-300 px-2 py-1 text-sm" maxLength={1000} />
            {next[inc.status].map((a) => <Button key={a} onClick={() => act.mutate(a)} disabled={act.isPending}>{a.charAt(0).toUpperCase() + a.slice(1)}</Button>)}
          </div>
          {act.error && <MutationError error={act.error} />}
        </div>
      )}
    </li>
  );
}

function Evaluations({ items }: { items: OpsAlertEval[] }) {
  return (
    <Card title="Latest alert evaluation">
      <table className="w-full text-sm" data-testid="eval-table"><thead className="text-left text-xs text-slate-500"><tr><th>Rule</th><th>Status</th><th>Observed</th><th>Threshold</th><th>Explanation</th></tr></thead>
        <tbody>{items.map((e) => (
          <tr key={e.rule_id} className="border-t border-slate-100 align-top"><td className="py-1.5">{e.rule_id} <span className="text-xs text-slate-500">v{e.rule_version}</span></td>
            <td><Badge tone={e.status === "firing" ? "danger" : e.status === "ok" ? "success" : "neutral"}>{e.status === "no_data" ? "no data" : e.status}</Badge></td>
            <td>{e.observed == null ? "–" : Number(e.observed.toPrecision(3))}</td><td>{e.comparator} {e.threshold}</td><td className="text-xs text-slate-600">{e.explanation}</td></tr>))}</tbody></table>
    </Card>
  );
}

function Incidents() {
  const qc = useQueryClient();
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(1);
  const list = useQuery({ queryKey: ["ops-incidents", status, page], queryFn: () => api.opsIncidents({ status: status || undefined, page }) });
  const ev = useMutation({ mutationFn: api.opsEvaluate, onSuccess: () => { void qc.invalidateQueries({ queryKey: ["ops-incidents"] }); void qc.invalidateQueries({ queryKey: ["ops-overview"] }); } });
  return (
    <div className="space-y-4">
      <Card title="Alerts" actions={<Button onClick={() => ev.mutate()} disabled={ev.isPending}>{ev.isPending ? "Evaluating…" : "Evaluate alerts now"}</Button>}>
        <p className="text-sm text-slate-600">Rules use measured data only. Rules with too little data report “no data” instead of passing.</p>
        {ev.error && <MutationError error={ev.error} />}
      </Card>
      {ev.data && <Evaluations items={ev.data.evaluations} />}
      <Card title="Incidents" actions={<select aria-label="Status filter" value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }} className="rounded border border-slate-300 px-2 py-1 text-sm">
        <option value="">All</option>{["open", "acknowledged", "investigating", "resolved", "reopened"].map((s) => <option key={s} value={s}>{s}</option>)}</select>}>
        {list.isPending ? <Spinner /> : list.isError ? <ErrorState error={list.error} onRetry={() => void list.refetch()} /> : list.data.items.length === 0 ? (
          <EmptyState title="No incidents">An empty list does not prove the system is healthy; evaluate alerts to check the rules against current measurements.</EmptyState>
        ) : (
          <><ul className="space-y-2">{list.data.items.map((i) => <IncidentRow key={i.id} inc={i} />)}</ul><Pagination page={list.data.page} pages={list.data.pages} total={list.data.total} onPage={setPage} /></>
        )}
      </Card>
      <DiagnosticsPanel />
    </div>
  );
}

export function OperationsPage() {
  const [tab, setTab] = useState<Tab>("overview");
  return (
    <div>
      <PageHeader title="Operational intelligence" subtitle="Health, workflows, incidents, SLOs and investigation tools built from measurements NOVA recorded since this process started." />
      <div role="tablist" aria-label="Operations sections" className="mb-4 flex flex-wrap gap-1 border-b border-slate-200">
        {TABS.map(([id, label]) => (
          <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)} className={`rounded-t px-3 py-2 text-sm font-medium ${tab === id ? "bg-white text-sky-800 shadow-[inset_0_-2px_0_#0369a1]" : "text-slate-600 hover:text-slate-900"}`}>{label}</button>
        ))}
      </div>
      {tab === "overview" && <Overview />}
      {tab === "workflows" && <Workflows />}
      {tab === "incidents" && <Incidents />}
      {tab === "slos" && <SloPanel />}
      {tab === "investigate" && <InvestigationPanel />}
    </div>
  );
}
