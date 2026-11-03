import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router-dom";
import { Badge, Button, Card, EmptyState, ErrorState, Field, Notice, PageHeader, Pagination, Spinner, Stat, TextInput } from "../components/ui";
import { api } from "../services/api";
import { formatDate } from "../services/format";
import type { SecFinding, SecRemediation, SecScan } from "../types/api";
import { MutationError } from "./RepairsPage";
import { AdvisoriesPanel, InventoryPanel, PolicyPanel } from "./SecurityPanels";
import { DecisionBadge, PriorityBadge, ScanStateBadge, SeverityBadge, StatusBadge } from "./SecurityShared";
import { download } from "./WorkflowShared";

const SEVS = ["critical", "high", "medium", "low", "info"] as const;
const TABS = [["overview", "Overview"], ["findings", "Findings"], ["inventory", "Inventory"], ["advisories", "Advisories"], ["policy", "Policy gate"]] as const;
type Tab = (typeof TABS)[number][0];

function ScanCard({ repositoryId, scan, canWrite }: { repositoryId: string; scan: SecScan | null; canWrite: boolean }) {
  const qc = useQueryClient();
  const [types, setTypes] = useState<string[]>(["dependencies", "code", "secrets"]);
  const [excl, setExcl] = useState("");
  const run = useMutation({
    mutationFn: () => api.secStartScan(repositoryId, { scan_types: types, exclude_globs: excl.split("\n").map((x) => x.trim()).filter(Boolean) }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["sec-overview", repositoryId] }); },
  });
  const toggle = (t: string) => setTypes((cur) => (cur.includes(t) ? cur.filter((x) => x !== t) : [...cur, t]));
  const state = scan?.summary.state;
  return (
    <Card title="Security scan">
      {scan ? (
        <div className="space-y-2 text-sm" data-testid="scan-card">
          <div className="flex flex-wrap items-center gap-2">
            {scan.status === "queued" || scan.status === "running" ? <Badge tone="info">{scan.status}</Badge> : <ScanStateBadge state={state} />}
            <code className="text-xs">{scan.head_sha ? scan.head_sha.slice(0, 8) : "no git revision"}</code>{scan.dirty && <Badge tone="warning">uncommitted changes</Badge>}
            <span className="text-xs text-slate-500">{scan.finished_at ? formatDate(scan.finished_at) : "in progress"}</span>
          </div>
          {scan.summary.headline && <p>{scan.summary.headline}</p>}
          {scan.error && <Notice tone="danger">{scan.error}</Notice>}
          {(scan.summary.incomplete_reasons ?? []).length > 0 && (
            <Notice tone="warning"><b>Coverage gaps:</b><ul className="list-disc pl-5">{scan.summary.incomplete_reasons?.map((r) => <li key={r}>{r}</li>)}</ul></Notice>
          )}
        </div>
      ) : <EmptyState title="No scan has been run">Scans only read files; nothing from the repository is executed or installed.</EmptyState>}
      {canWrite ? (
        <form className="mt-3 space-y-2" onSubmit={(e) => { e.preventDefault(); run.mutate(); }}>
          <fieldset className="flex flex-wrap gap-4 text-sm"><legend className="sr-only">Scan types</legend>
            {["dependencies", "code", "secrets"].map((t) => <label key={t} className="inline-flex items-center gap-1"><input type="checkbox" checked={types.includes(t)} onChange={() => toggle(t)} /> {t}</label>)}
          </fieldset>
          <Field label="Exclude paths from the secrets scan (one glob per line)" htmlFor="sec-excl" hint="Exclusions are recorded in the scan coverage and the policy treats excluded files as unknown.">
            <textarea id="sec-excl" rows={2} value={excl} onChange={(e) => setExcl(e.target.value)} className="w-full rounded border border-slate-300 p-1.5 text-xs" />
          </Field>
          <Button type="submit" disabled={run.isPending || types.length === 0 || scan?.status === "running" || scan?.status === "queued"}>{run.isPending ? "Starting…" : "Run scan"}</Button>
          {run.error && <MutationError error={run.error} />}
        </form>
      ) : <Notice>Your role can read results but not start scans.</Notice>}
      <p className="mt-2 text-xs text-slate-500">Detection is best-effort and not exhaustive. NOVA never tests, revokes or rotates credentials.</p>
    </Card>
  );
}

function RemediationBox({ repositoryId, finding, canWrite }: { repositoryId: string; finding: SecFinding; canWrite: boolean }) {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["sec-rem", repositoryId, finding.id], queryFn: () => api.secRemediations(repositoryId, finding.id) });
  const refresh = () => void qc.invalidateQueries({ queryKey: ["sec-rem", repositoryId, finding.id] });
  const propose = useMutation({ mutationFn: () => api.secPropose(repositoryId, finding.id), onSuccess: refresh });
  const submit = useMutation({ mutationFn: (id: string) => api.secSubmitRemediation(repositoryId, id), onSuccess: refresh });
  const latest: SecRemediation | undefined = list.data?.[0];
  const active = latest && (latest.status === "proposed" || latest.status === "submitted");
  return (
    <div className="space-y-2" data-testid="remediation">
      <h4 className="font-medium">Remediation</h4>
      {latest ? (
        <div className="space-y-1 text-sm">
          <div className="flex items-center gap-2"><Badge tone={latest.status === "cannot_fix" || latest.status === "validation_failed" ? "warning" : "info"}>{latest.status.replace("_", " ")}</Badge><span>{latest.proposal.change ?? latest.proposal.reason}</span></div>
          {latest.status === "cannot_fix" && <p className="text-xs text-slate-600">{latest.proposal.reason}</p>}
          {(latest.proposal.caveats ?? []).map((c) => <p key={c} className="text-xs text-slate-600">• {c}</p>)}
          {(latest.proposal.side_effects ?? []).map((c) => <p key={c} className="text-xs text-slate-600">• {c}</p>)}
          {latest.status === "validation_failed" && <Notice tone="warning">Validation failed; no file was changed. {latest.validation?.note}</Notice>}
          {latest.status === "submitted" && <Notice tone="success">Validated as a repair patch. Nothing has been applied: review, approve and apply it in <a className="underline" href={`/repositories/${repositoryId}/repairs`}>Repair intelligence</a>.</Notice>}
          {latest.diff_text && <pre className="max-h-48 overflow-auto rounded bg-slate-50 p-2 text-xs">{latest.diff_text}</pre>}
          {canWrite && latest.status === "proposed" && <Button disabled={submit.isPending} onClick={() => submit.mutate(latest.id)}>{submit.isPending ? "Validating…" : "Create repair patch and validate"}</Button>}
        </div>
      ) : <p className="text-xs text-slate-600">No proposal yet. Proposals are generated only where advisory data or a deterministic rewrite supports them, and never change files by themselves.</p>}
      {canWrite && !active && <Button variant="secondary" disabled={propose.isPending} onClick={() => propose.mutate()}>{propose.isPending ? "Preparing…" : "Propose a fix"}</Button>}
      {(propose.error || submit.error) && <MutationError error={propose.error ?? submit.error} />}
    </div>
  );
}

function FindingDetail({ repositoryId, id, canWrite }: { repositoryId: string; id: string; canWrite: boolean }) {
  const qc = useQueryClient();
  const d = useQuery({ queryKey: ["sec-finding", repositoryId, id], queryFn: () => api.secFinding(repositoryId, id) });
  const [reason, setReason] = useState("");
  const refresh = () => { void qc.invalidateQueries({ queryKey: ["sec-finding", repositoryId, id] }); void qc.invalidateQueries({ queryKey: ["sec-findings"] }); void qc.invalidateQueries({ queryKey: ["sec-overview", repositoryId] }); };
  const triage = useMutation({ mutationFn: (status: string) => api.secTriage(repositoryId, id, { status, reason }), onSuccess: () => { setReason(""); refresh(); } });
  const suppress = useMutation({ mutationFn: () => api.secSuppress(repositoryId, { finding_id: id, reason }), onSuccess: () => { setReason(""); refresh(); void qc.invalidateQueries({ queryKey: ["sec-supp", repositoryId] }); } });
  if (d.isLoading) return <Spinner />;
  if (d.error || !d.data) return <ErrorState error={d.error} />;
  const f = d.data;
  const open = f.status === "open";
  return (
    <div className="space-y-3 text-sm" data-testid="finding-detail">
      <div className="flex flex-wrap items-center gap-2"><h3 className="font-semibold">{f.title}</h3><SeverityBadge severity={f.severity} /><PriorityBadge priority={f.priority} /><StatusBadge status={f.status} /></div>
      <p className="text-xs text-slate-600">{f.rel_path ?? "no file"}{f.line ? `:${f.line}` : ""} · {f.rule_id} · confidence {f.confidence} · evidence {f.evidence_quality.replace("_", " ")}</p>
      <p>{f.description}</p>
      {f.preview && <p className="text-xs">Redacted preview: <code>{f.preview}</code> (the value itself is never stored or shown)</p>}
      {f.evidence.assessment && <Notice>{f.evidence.assessment}</Notice>}
      {f.priority.factors && <details className="text-xs"><summary className="cursor-pointer">How the priority was computed ({f.priority.model})</summary><ul className="list-disc pl-5">{f.priority.factors.map((x) => <li key={x}>{x}</li>)}</ul></details>}
      <div><h4 className="font-medium">Suggested remediation</h4><p className="text-xs text-slate-700">{f.remediation}</p></div>
      <RemediationBox repositoryId={repositoryId} finding={f} canWrite={canWrite} />
      {canWrite && (open || f.status === "accepted_risk" || f.status === "false_positive") && (
        <div className="space-y-2 rounded border border-slate-200 p-2">
          <Field label="Reason (required to change status)" htmlFor="tri-reason"><TextInput id="tri-reason" value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
          <div className="flex flex-wrap gap-2">
            {open && <Button variant="secondary" disabled={reason.length < 8 || triage.isPending} onClick={() => triage.mutate("false_positive")}>Mark false positive</Button>}
            {open && <Button variant="secondary" disabled={reason.length < 8 || triage.isPending} onClick={() => triage.mutate("accepted_risk")}>Accept risk</Button>}
            {open && <Button variant="secondary" disabled={reason.length < 8 || suppress.isPending} onClick={() => suppress.mutate()}>Suppress this finding</Button>}
            {!open && <Button variant="secondary" disabled={triage.isPending} onClick={() => triage.mutate("open")}>Reopen</Button>}
          </div>
          {(triage.error || suppress.error) && <MutationError error={triage.error ?? suppress.error} />}
        </div>
      )}
      <div><h4 className="font-medium">History</h4><ol className="text-xs text-slate-600">{f.events.map((e, i) => <li key={`${e.created_at}-${i}`}>{formatDate(e.created_at)} · {e.action}{e.to_status ? ` → ${e.to_status}` : ""}{e.reason ? ` · ${e.reason}` : ""}</li>)}</ol></div>
    </div>
  );
}

function FindingsPanel({ repositoryId, canWrite }: { repositoryId: string; canWrite: boolean }) {
  const [kind, setKind] = useState("");
  const [severity, setSeverity] = useState("");
  const [status, setStatus] = useState("open");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState("priority");
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<string | null>(null);
  const list = useQuery({ queryKey: ["sec-findings", repositoryId, kind, severity, status, q, sort, page], queryFn: () => api.secFindings(repositoryId, { kind: kind || undefined, severity: severity || undefined, status: status || undefined, q: q || undefined, sort, page }) });
  const sel = (setter: (v: string) => void) => (e: React.ChangeEvent<HTMLSelectElement>) => { setter(e.target.value); setPage(1); };
  const cls = "rounded border border-slate-300 p-1.5 text-sm";
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card title="Findings">
        <div className="mb-3 flex flex-wrap items-end gap-2">
          <Field label="Kind" htmlFor="ff-kind"><select id="ff-kind" value={kind} onChange={sel(setKind)} className={cls}><option value="">all</option><option value="code">code</option><option value="secret">secret</option><option value="dependency">dependency</option></select></Field>
          <Field label="Severity" htmlFor="ff-sev"><select id="ff-sev" value={severity} onChange={sel(setSeverity)} className={cls}><option value="">all</option>{SEVS.map((s) => <option key={s}>{s}</option>)}</select></Field>
          <Field label="Status" htmlFor="ff-status"><select id="ff-status" value={status} onChange={sel(setStatus)} className={cls}><option value="">all</option>{["open", "resolved", "false_positive", "accepted_risk", "suppressed"].map((s) => <option key={s}>{s}</option>)}</select></Field>
          <Field label="Sort" htmlFor="ff-sort"><select id="ff-sort" value={sort} onChange={sel(setSort)} className={cls}><option value="priority">priority</option><option value="severity">severity</option><option value="path">path</option><option value="last_seen">last seen</option></select></Field>
          <Field label="Search title" htmlFor="ff-q"><TextInput id="ff-q" value={q} onChange={(e) => { setQ(e.target.value); setPage(1); }} /></Field>
        </div>
        {list.isLoading ? <Spinner /> : list.error ? <ErrorState error={list.error} /> : !list.data || list.data.items.length === 0 ? <EmptyState title="No findings match">If no scan has run, run one first. Zero findings from a partial scan is not a clean result.</EmptyState> : (
          <>
            <ul className="divide-y divide-slate-100" aria-label="Findings">
              {list.data.items.map((f) => (
                <li key={f.id} className="py-2">
                  <button type="button" className={`w-full text-left text-sm ${selected === f.id ? "font-semibold" : ""}`} onClick={() => setSelected(f.id)} data-testid={`finding-${f.id}`}>
                    <span className="mr-2 inline-flex flex-wrap items-center gap-1"><PriorityBadge priority={f.priority} /><SeverityBadge severity={f.severity} /><StatusBadge status={f.status} /></span>
                    {f.title} <span className="text-xs text-slate-500">{f.rel_path}{f.line ? `:${f.line}` : ""}</span>{f.preview && <code className="ml-1 text-xs">{f.preview}</code>}
                  </button>
                </li>
              ))}
            </ul>
            <Pagination page={list.data.page} pages={list.data.pages} total={list.data.total} onPage={setPage} />
          </>
        )}
      </Card>
      <Card title="Finding detail">{selected ? <FindingDetail repositoryId={repositoryId} id={selected} canWrite={canWrite} /> : <EmptyState title="Select a finding">Details, evidence, history and remediation appear here.</EmptyState>}</Card>
    </div>
  );
}

export function SecurityPage() {
  const { repositoryId = "" } = useParams();
  const [tab, setTab] = useState<Tab>("overview");
  const overview = useQuery({ queryKey: ["sec-overview", repositoryId], queryFn: () => api.secOverview(repositoryId), refetchInterval: (q) => (q.state.data?.latest_scan && ["queued", "running"].includes(q.state.data.latest_scan.status) ? 1500 : false) });
  const [reportErr, setReportErr] = useState<unknown>(null);
  const report = useMutation({
    mutationFn: async (format: string) => { const r = await api.secMakeReport(repositoryId, format); return api.secReport(repositoryId, r.id); },
    onSuccess: (r) => { setReportErr(null); download(`security-report-${r.id}.${r.format === "markdown" ? "md" : r.format}`, r.body ?? "", r.format === "json" ? "application/json" : r.format === "html" ? "text/html" : "text/plain"); },
    onError: setReportErr,
  });
  if (overview.isLoading) return <Spinner />;
  if (overview.error || !overview.data) return <ErrorState error={overview.error} onRetry={() => void overview.refetch()} />;
  const o = overview.data;
  const scan = o.latest_scan;
  const done = o.latest_completed_scan;
  return (
    <div className="space-y-4">
      <PageHeader title="Security Intelligence" subtitle="Supply-chain inventory, advisories, security rules, secrets triage and a policy gate" />
      <div role="tablist" aria-label="Security sections" className="flex flex-wrap gap-1 border-b border-slate-200">
        {TABS.map(([id, label]) => <button key={id} role="tab" type="button" aria-selected={tab === id} onClick={() => setTab(id)} className={`px-3 py-1.5 text-sm ${tab === id ? "border-b-2 border-sky-700 font-semibold" : "text-slate-600"}`}>{label}</button>)}
      </div>
      {tab === "overview" && (
        <div className="space-y-4">
          {o.notice && <Notice>{o.notice}</Notice>}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">{SEVS.map((s) => <Stat key={s} label={`Open ${s}`} value={o.open_by_severity[s]} />)}</div>
          <div className="grid gap-4 lg:grid-cols-2">
            <ScanCard repositoryId={repositoryId} scan={scan} canWrite={o.can_write} />
            <Card title="Policy decision">
              {o.latest_evaluation ? (
                <div className="space-y-1 text-sm" data-testid="latest-evaluation"><DecisionBadge status={o.latest_evaluation.status} /><p>{o.latest_evaluation.explanation}</p><p className="text-xs text-slate-500">policy v{o.latest_evaluation.policy_version} · {formatDate(o.latest_evaluation.created_at)}</p></div>
              ) : <p className="text-sm text-slate-600">No policy evaluation yet. Open the Policy gate tab to evaluate the latest scan.</p>}
              <div className="mt-3 flex flex-wrap gap-2">
                {done && o.can_write && (["json", "markdown", "html", "csv"] as const).map((f) => <Button key={f} variant="secondary" disabled={report.isPending} onClick={() => report.mutate(f)}>Report ({f})</Button>)}
              </div>
              {reportErr !== null && <MutationError error={reportErr} />}
            </Card>
          </div>
          <Card title="Advisory sources">
            <ul className="divide-y divide-slate-100 text-sm">{o.providers.map((p) => <li key={p.name} className="flex flex-wrap items-center gap-2 py-1.5"><b>{p.name}</b><Badge tone={p.enabled ? "success" : "neutral"}>{p.enabled ? "enabled" : "off"}</Badge><span className="text-xs text-slate-600">{p.detail}</span></li>)}</ul>
          </Card>
        </div>
      )}
      {tab === "findings" && <FindingsPanel repositoryId={repositoryId} canWrite={o.can_write} />}
      {tab === "inventory" && <InventoryPanel repositoryId={repositoryId} scan={done} />}
      {tab === "advisories" && <AdvisoriesPanel repositoryId={repositoryId} />}
      {tab === "policy" && <PolicyPanel repositoryId={repositoryId} canAdmin={o.can_admin} canWrite={o.can_write} />}
    </div>
  );
}
