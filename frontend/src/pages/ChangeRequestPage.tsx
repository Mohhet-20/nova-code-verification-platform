import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { RequestGovernanceReview } from "../components/RequestGovernanceReview";
import { Badge, Button, Card, ErrorState, Notice, PageHeader, Spinner } from "../components/ui";
import { api } from "../services/api";
import { formatDate } from "../services/format";
import type { CiWorkflowRun } from "../types/api";
import { Confirm, MutationError } from "./RepairsPage";
import { GateBadge, shortSha } from "./VerificationShared";
import { download, REPORT_TONE, ReadinessBadge } from "./WorkflowShared";

const ruleTone = (s: string) => (s === "pass" ? "success" : s === "fail" ? "danger" : "warning");

function ReadinessCard({ repositoryId, wf }: { repositoryId: string; wf: CiWorkflowRun }) {
  const r = useQuery({ queryKey: ["ci-readiness", repositoryId, wf.id], queryFn: () => api.ciReadiness(repositoryId, wf.id) });
  if (r.isLoading) return <Spinner />;
  if (r.error || !r.data) return <ErrorState error={r.error} />;
  const d = r.data.decision;
  return (
    <Card title="Release readiness">
      <div className="flex flex-wrap items-center gap-2" data-testid="readiness"><ReadinessBadge outcome={r.data.outcome} stale={r.data.stale} />{d && <code className="text-xs">{shortSha(d.revision)}</code>}</div>
      {!d && <p className="mt-2 text-sm text-slate-600">{r.data.reason}</p>}
      {d && (
        <>
          <p className="mt-2 text-sm">{d.reason}</p>
          <p className="text-xs text-slate-500">{d.meaning}</p>
          <ul className="mt-2 divide-y divide-slate-100 text-sm" aria-label="Readiness rules">
            {d.rules.map((x) => <li key={x.id} className="flex flex-wrap items-center gap-2 py-1"><Badge tone={ruleTone(x.status)}>{x.status}</Badge><code>{x.id}</code><span className="text-slate-700">{x.detail}</span></li>)}
          </ul>
        </>
      )}
    </Card>
  );
}

function StatusReports({ repositoryId, wf, canWrite }: { repositoryId: string; wf: CiWorkflowRun; canWrite: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["ci-status", repositoryId, wf.id], queryFn: () => api.ciStatusReports(repositoryId, wf.id) });
  const refresh = () => void qc.invalidateQueries({ queryKey: ["ci-status", repositoryId, wf.id] });
  const send = useMutation({ mutationFn: () => api.ciSendStatus(repositoryId, wf.id), onSuccess: refresh });
  const retry = useMutation({ mutationFn: (id: string) => api.ciRetryStatus(repositoryId, id), onSuccess: refresh });
  return (
    <Card title="External status reporting" actions={canWrite ? <Button variant="secondary" disabled={send.isPending} onClick={() => send.mutate()}>Report status</Button> : undefined}>
      {q.isLoading ? <Spinner /> : !q.data || q.data.length === 0 ? <p className="text-sm text-slate-600">Nothing has been reported to the provider for this run.</p> : (
        <ul className="divide-y divide-slate-100 text-sm" aria-label="Status reports">
          {q.data.map((s) => (
            <li key={s.id} className="flex flex-wrap items-center gap-2 py-2">
              <Badge tone={REPORT_TONE[s.status] ?? "neutral"}>{s.status}</Badge><code>{s.context}</code><span>{s.state}</span><code className="text-xs">{shortSha(s.head_sha)}</code>
              <span className="text-xs text-slate-500">attempt {s.attempts}</span>
              {s.error && <span className="text-xs text-amber-700">{s.error}</span>}
              {canWrite && (s.status === "retry_scheduled" || s.status === "unconfigured") && <Button variant="secondary" disabled={retry.isPending} onClick={() => retry.mutate(s.id)}>Retry</Button>}
            </li>
          ))}
        </ul>
      )}
      <MutationError error={send.error ?? retry.error} />
    </Card>
  );
}

export function ChangeRequestPage() {
  const { repositoryId = "", crId = "" } = useParams();
  const qc = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const cr = useQuery({ queryKey: ["ci-cr", repositoryId, crId], queryFn: () => api.ciChangeRequest(repositoryId, crId), refetchInterval: (qq) => (qq.state.data?.verification.latest_run_id && !qq.state.data.verification.current_revision_verified ? 4000 : false) });
  const runs = useQuery({ queryKey: ["ci-runs", repositoryId, crId], queryFn: () => api.ciWorkflows(repositoryId, crId) });
  const report = useQuery({ queryKey: ["ci-review", repositoryId, crId, cr.data?.head_sha], queryFn: () => api.ciReviewReport(repositoryId, crId), enabled: Boolean(cr.data) });
  const ov = useQuery({ queryKey: ["ci-overview", repositoryId], queryFn: () => api.ciOverview(repositoryId) });
  const refresh = () => { for (const k of ["ci-cr", "ci-runs", "ci-review", "ci-readiness", "ci-status"]) void qc.invalidateQueries({ queryKey: [k] }); };
  const start = useMutation({ mutationFn: () => api.ciStartWorkflow(repositoryId, crId, { confirm: true }), onSuccess: () => { setConfirming(false); refresh(); } });
  const make = useMutation({ mutationFn: (kind: "review" | "release") => api.ciMakeReport(repositoryId, crId, kind).then((s) => api.ciReport(repositoryId, s.id)) });
  if (cr.isLoading) return <Spinner />;
  if (cr.error || !cr.data) return <ErrorState error={cr.error} onRetry={() => void cr.refetch()} />;
  const c = cr.data;
  const wf = runs.data?.find((w) => w.id === selected) ?? runs.data?.[0];
  const canWrite = ov.data?.can_admin !== false;
  const rep = report.data;
  return (
    <div className="space-y-4">
      <PageHeader title={c.title || c.source_branch} subtitle={`${c.provider} ${c.external_id} · ${c.source_branch} → ${c.target_branch}`} actions={<><Link className="text-sm text-brand-700 hover:underline" to={`/repositories/${repositoryId}/workflow`}>All change requests</Link><RequestGovernanceReview resourceType="change_request" resourceId={c.id} /></>} />
      <Card title="Revision">
        <p className="text-sm">Head <code>{shortSha(c.head_sha)}</code> · base <code>{shortSha(c.base_sha)}</code> · {c.state} · {c.files_count} changed file(s) (source: {c.files_source})</p>
        {c.verification.stale && <Notice tone="warning">A newer commit landed after the last verification. Earlier results do not apply to the current revision.</Notice>}
        {!c.verification.current_revision_verified && !c.verification.stale && <Notice>The current revision has not been verified.</Notice>}
        {c.verification.current_revision_verified && <p className="mt-1 text-sm text-slate-600">The current revision has a completed verification.</p>}
        {(c.notes ?? []).map((n) => <Notice key={n}>{n}</Notice>)}
        <div className="mt-3">
          {confirming ? (
            <Confirm title="Run verification for this revision?" confirmLabel="Run verification" busy={start.isPending} onConfirm={() => start.mutate()} onCancel={() => setConfirming(false)}>
              This executes the repository’s tests locally against the checked-out revision <code>{shortSha(c.head_sha)}</code>. Pull-request code is never executed automatically.
            </Confirm>
          ) : <Button disabled={c.state !== "open"} onClick={() => setConfirming(true)}>Start verification</Button>}
          <MutationError error={start.error} />
        </div>
      </Card>
      <Card title="Verification workflow runs">
        {!runs.data || runs.data.length === 0 ? <p className="text-sm text-slate-600">No workflow runs yet.</p> : (
          <ul className="divide-y divide-slate-100 text-sm" aria-label="Workflow runs">
            {runs.data.map((w) => (
              <li key={w.id} className="flex flex-wrap items-center gap-2 py-2">
                <button className="underline" onClick={() => setSelected(w.id)}>{formatDate(w.created_at)}</button>
                <code className="text-xs">{shortSha(w.head_sha)}</code><Badge>{w.status}</Badge><GateBadge outcome={w.gate_outcome} status={w.status} />
                <ReadinessBadge outcome={w.readiness_effective} stale={w.stale} />
                {w.cv_run_id && <Link className="text-brand-700 hover:underline" to={`/repositories/${repositoryId}/verification/${w.cv_run_id}`}>Run details</Link>}
              </li>
            ))}
          </ul>
        )}
      </Card>
      {wf && wf.status !== "awaiting" && <ReadinessCard repositoryId={repositoryId} wf={wf} />}
      {wf && <StatusReports repositoryId={repositoryId} wf={wf} canWrite={canWrite} />}
      {rep && (
        <Card title="Unified review report">
          <div className="space-y-2 text-sm" data-testid="review-report">
            <p>Evidence is for the current revision: <strong>{rep.revision.evidence_is_current ? "yes" : "NO"}</strong> · quality gate: {rep.quality_gate ? <Badge>{rep.quality_gate.outcome}</Badge> : "none"}</p>
            <Notice tone="warning">{rep.security_analysis.note}</Notice>
            <h3 className="font-semibold">Checks</h3>
            <ul>{rep.checks.length === 0 ? <li className="text-slate-600">No checks recorded for the current revision.</li> : rep.checks.map((k) => <li key={k.check_id}><code>{k.check_id}</code> — {k.status}{k.required ? " (required)" : " (optional)"}: {k.detail}</li>)}</ul>
            <h3 className="font-semibold">Findings</h3>
            <ul>{rep.findings.length === 0 ? <li className="text-slate-600">No new static-analysis findings were recorded (this is not a security conclusion).</li> : rep.findings.map((f, i) => <li key={`${f.rule_id}-${i}`}>[{f.severity}] {f.title} in <code>{f.location.path}</code></li>)}</ul>
            <h3 className="font-semibold">Gaps and limits</h3>
            <ul className="list-disc pl-5">{[...rep.gaps, ...rep.limitations].map((g) => <li key={g}>{g}</li>)}</ul>
          </div>
        </Card>
      )}
      <Card title="Export">
        <div className="flex flex-wrap gap-2">
          <Button variant="secondary" disabled={make.isPending} onClick={() => make.mutate("review")}>Generate review report</Button>
          <Button variant="secondary" disabled={make.isPending || !wf} onClick={() => make.mutate("release")}>Generate release evidence</Button>
        </div>
        <MutationError error={make.error} />
        {make.data && (
          <div className="mt-3 space-y-1 text-sm" data-testid="generated-report">
            <p>Report <code>{make.data.sha256.slice(0, 16)}</code> · {make.data.size_bytes} bytes{make.data.truncated ? " · truncated to fit the size limit" : ""}</p>
            <div className="flex gap-2">
              <Button variant="secondary" onClick={() => download(`nova-${make.data.kind}-${shortSha(make.data.head_sha)}.json`, JSON.stringify(make.data.body, null, 2), "application/json")}>Download JSON</Button>
              <Button variant="secondary" onClick={() => download(`nova-${make.data.kind}-${shortSha(make.data.head_sha)}.md`, make.data.markdown, "text/markdown")}>Download Markdown</Button>
            </div>
            <pre className="max-h-64 overflow-auto rounded border border-slate-200 bg-slate-50 p-2 text-xs whitespace-pre-wrap">{make.data.markdown}</pre>
          </div>
        )}
      </Card>
    </div>
  );
}
