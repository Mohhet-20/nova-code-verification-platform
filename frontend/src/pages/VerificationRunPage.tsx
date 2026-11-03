import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router-dom";
import { Badge, Button, ButtonLink, Card, EmptyState, ErrorState, Notice, PageHeader, Spinner } from "../components/ui";
import { api } from "../services/api";
import { formatDate } from "../services/format";
import type { CvCheck, CvRegression, CvRunDetail } from "../types/api";
import { MutationError } from "./RepairsPage";
import { CATEGORY_TONE, CHECK_TONE, GateBadge, RUN_TONE, shortSha } from "./VerificationShared";

const ACTIVE = ["queued", "running"];
type Tab = "checks" | "regressions" | "gate" | "compare" | "changes" | "events";
const TABS: [Tab, string][] = [["checks", "Checks"], ["regressions", "Regressions"], ["gate", "Quality gate"], ["compare", "Baseline comparison"], ["changes", "Change set"], ["events", "Events"]];

function Checks({ checks }: { checks: CvCheck[] }) {
  if (checks.length === 0) return <EmptyState title="No checks were planned" />;
  return (
    <ul className="divide-y divide-slate-200" aria-label="Checks">
      {checks.map((c) => (
        <li key={c.id} className="py-2 text-sm" data-testid={`check-${c.check_id}`}>
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={CHECK_TONE[c.status] ?? "neutral"}>{c.status.replace("_", " ")}</Badge><code>{c.check_id}</code>
            <span className="text-xs text-slate-500">tier {c.tier} · {c.required ? "required" : "optional"} · confidence {c.confidence}</span>
          </div>
          <p className="text-slate-700">{c.reason}</p>
          {typeof c.result.detail === "string" && <p className="text-slate-900">{c.result.detail}</p>}
          {c.prerequisites.length > 0 && <p className="text-xs text-slate-500">Prerequisites: {c.prerequisites.join(", ")}</p>}
          {(c.started_at || c.finished_at) && <p className="text-xs text-slate-500">{formatDate(c.started_at)} → {formatDate(c.finished_at)}</p>}
          {Array.isArray(c.result.failures) && c.result.failures.length > 0 && (
            <details className="mt-1"><summary className="cursor-pointer text-xs">Failures ({c.result.failures.length})</summary>
              <ul className="list-disc pl-5">{(c.result.failures as { test?: string; message?: string }[]).slice(0, 20).map((f, i) => <li key={i}><code>{f.test}</code> {f.message}</li>)}</ul>
            </details>
          )}
        </li>
      ))}
    </ul>
  );
}

export function RegressionList({ reg }: { reg: CvRegression | null }) {
  if (!reg) return <EmptyState title="No regression analysis">The run has not produced test results.</EmptyState>;
  if (!reg.available) return <Notice tone="warning">Regression analysis is unavailable: {reg.reason ?? "no compatible baseline or no test results."} Failures cannot be classified as new or pre-existing.</Notice>;
  const items = reg.items ?? [];
  return (
    <div className="space-y-2 text-sm">
      <p aria-label="Regression counts">{Object.entries(reg.counts ?? {}).map(([k, v]) => `${k.replace("_", "-")}: ${v}`).join(" · ")}</p>
      {reg.baseline && !reg.baseline.compatible && <Notice tone="warning">The baseline is not compatible: {reg.baseline.problems.join("; ")}</Notice>}
      {items.length === 0 ? <p className="text-slate-600">No failing or resolved tests.</p> : (
        <ul className="divide-y divide-slate-200" aria-label="Regression findings">
          {items.map((i) => (
            <li key={`${i.test}-${i.category}`} className="py-2">
              <div className="flex flex-wrap items-center gap-2"><Badge tone={CATEGORY_TONE[i.category]}>{i.category.replace("_", "-")}</Badge><code>{i.test}</code></div>
              <p>{i.explanation}</p>
              {i.message && <pre className="mt-1 max-h-32 overflow-auto rounded bg-slate-50 p-2 text-xs">{i.message}</pre>}
            </li>
          ))}
        </ul>
      )}
      {reg.basis && <p className="text-xs text-slate-500">{reg.basis}</p>}
    </div>
  );
}

function Gate({ run }: { run: CvRunDetail }) {
  const g = run.gate;
  if (!g) return <Notice tone="info">No decision yet: the run is {run.status}. A decision is never assumed.</Notice>;
  return (
    <div className="space-y-2 text-sm">
      <p><strong>{g.outcome}</strong> — {g.reason}</p>
      <p className="text-xs text-slate-500">Policy version {run.policy_version}. Precedence: {g.precedence.join(" > ")}. {g.meaning}</p>
      <table className="w-full text-left" aria-label="Gate criteria">
        <thead><tr className="text-xs text-slate-500"><th className="py-1">Criterion</th><th>Result</th><th>Required</th><th>Evidence</th></tr></thead>
        <tbody>{g.rules.map((r) => (
          <tr key={r.id} className="border-t border-slate-100 align-top"><td className="py-1"><code>{r.id}</code></td><td><Badge tone={CHECK_TONE[r.status] ?? "neutral"}>{r.status}</Badge></td><td>{r.required ? "yes" : "no"}</td><td>{r.detail}</td></tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function Compare({ repositoryId, run }: { repositoryId: string; run: CvRunDetail }) {
  const [bid, setBid] = useState(run.baseline_id ?? "");
  const baselines = useQuery({ queryKey: ["cv-baselines", repositoryId], queryFn: () => api.cvBaselines(repositoryId) });
  const cmp = useQuery({ queryKey: ["cv-compare", repositoryId, run.id, bid], queryFn: () => api.cvCompare(repositoryId, run.id, bid), enabled: Boolean(bid) });
  return (
    <div className="space-y-2 text-sm">
      <label>Baseline <select aria-label="Baseline" value={bid} onChange={(e) => setBid(e.target.value)} className="ml-1 rounded border border-slate-300 p-1"><option value="">Choose…</option>{(baselines.data ?? []).map((b) => <option key={b.id} value={b.id}>{b.label || b.id.slice(0, 8)} @ {shortSha(b.head_sha)}</option>)}</select></label>
      {!bid ? <p className="text-slate-600">Pick a baseline to recompute the comparison. This does not change the recorded decision.</p> : cmp.isLoading ? <Spinner /> : cmp.isError ? <ErrorState error={cmp.error} /> : cmp.data && (
        <>
          {!cmp.data.compatibility.compatible && <Notice tone="warning">Not comparable: {cmp.data.compatibility.problems.join("; ")}</Notice>}
          <RegressionList reg={cmp.data.regression} />
          <p className="text-xs text-slate-500">{cmp.data.note}</p>
        </>
      )}
    </div>
  );
}

function Events({ repositoryId, runId }: { repositoryId: string; runId: string }) {
  const ev = useQuery({ queryKey: ["cv-events", repositoryId, runId], queryFn: () => api.cvEvents(repositoryId, runId) });
  if (ev.isLoading) return <Spinner />;
  if (ev.isError) return <ErrorState error={ev.error} />;
  return <ol className="space-y-1 text-sm" aria-label="Events">{ev.data!.map((e) => <li key={e.id}><span className="text-xs text-slate-500">{formatDate(e.created_at)}</span> <code>{e.kind}</code> {Object.keys(e.detail).length > 0 && <span className="text-xs text-slate-600">{JSON.stringify(e.detail).slice(0, 160)}</span>}</li>)}</ol>;
}

export function VerificationRunPage() {
  const { repositoryId = "", runId = "" } = useParams();
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>("checks");
  const run = useQuery({ queryKey: ["cv-run", repositoryId, runId], queryFn: () => api.cvRun(repositoryId, runId), refetchInterval: (q) => (q.state.data && ACTIVE.includes(q.state.data.status) ? 1500 : false) });
  const cancel = useMutation({ mutationFn: () => api.cvCancel(repositoryId, runId), onSuccess: () => { void qc.invalidateQueries({ queryKey: ["cv-run", repositoryId, runId] }); } });
  const r = run.data;
  const back = <ButtonLink to={`/repositories/${repositoryId}/verification`} variant="secondary">Back to Continuous Verification</ButtonLink>;
  if (run.isLoading) return <><PageHeader title="Verification run" actions={back} /><Spinner label="Loading run…" /></>;
  if (run.isError || !r) return <><PageHeader title="Verification run" actions={back} /><ErrorState error={run.error} onRetry={() => void run.refetch()} /></>;
  const active = ACTIVE.includes(r.status);
  return (
    <>
      <PageHeader title={`${r.kind === "baseline" ? "Baseline" : "Verification"} run · ${r.mode}`} subtitle={`${r.source.replace("_", " ")} · ${r.files_changed} changed file(s) · started ${formatDate(r.started_at ?? r.created_at)}`} actions={back} />
      <Card>
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <GateBadge outcome={r.gate_outcome} status={r.status} freshness={r.freshness} />
          <Badge tone={RUN_TONE[r.status] ?? "neutral"}>{r.status}{r.cancel_requested && r.status !== "cancelled" ? " (cancelling)" : ""}</Badge>
          <span>HEAD <code>{shortSha(r.head_sha)}</code></span>
          <span>State <code title="Fingerprint of the exact source state verified">{shortSha(r.tree_fingerprint)}</code></span>
          <span>Policy v{r.policy_version}</span>
          {active && <Button variant="secondary" disabled={cancel.isPending || r.cancel_requested} onClick={() => cancel.mutate()}>{r.cancel_requested ? "Cancelling…" : "Cancel run"}</Button>}
        </div>
        {r.reason && <p className="mt-2 text-sm text-slate-700">{r.reason}</p>}
        <MutationError error={cancel.error} />
        {active && <p className="mt-2 text-sm text-slate-600" role="status">Running… this page refreshes automatically. Partial results are kept if the run is cancelled.</p>}
      </Card>
      {r.freshness.stale === true && <Notice tone="warning"><span data-testid="stale-run">This result is stale: {r.freshness.reason ?? "the source changed after the run."} It describes the earlier state above, not the current one; run verification again.</span></Notice>}
      {r.freshness.stale === null && r.status === "completed" && <Notice tone="info">Whether the source has changed since this run could not be determined.</Notice>}
      <div role="tablist" aria-label="Run sections" className="flex flex-wrap gap-2">
        {TABS.map(([k, label]) => <button key={k} role="tab" aria-selected={tab === k} onClick={() => setTab(k)} className={`rounded border px-3 py-1 text-sm ${tab === k ? "border-slate-900 bg-slate-900 text-white" : "border-slate-300"}`}>{label}</button>)}
      </div>
      <Card>
        {tab === "checks" && <Checks checks={r.checks} />}
        {tab === "regressions" && <RegressionList reg={r.regression} />}
        {tab === "gate" && <Gate run={r} />}
        {tab === "compare" && <Compare repositoryId={repositoryId} run={r} />}
        {tab === "changes" && (
          <div className="space-y-1 text-sm">
            {r.change_set.warnings.map((w) => <Notice key={w} tone="warning">{w}</Notice>)}
            <ul aria-label="Changed files" className="divide-y divide-slate-100">{r.change_set.files.map((f) => <li key={f.path} className="flex gap-2 py-1"><Badge>{f.status}</Badge><code>{f.path}</code></li>)}</ul>
            {r.change_set.files.length === 0 && <p className="text-slate-600">No changed files were detected.</p>}
            {r.plan.notes.map((n) => <p key={n} className="text-xs text-slate-500">{n}</p>)}
          </div>
        )}
        {tab === "events" && <Events repositoryId={repositoryId} runId={runId} />}
      </Card>
    </>
  );
}
