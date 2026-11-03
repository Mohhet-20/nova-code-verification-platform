import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { useParams } from "react-router-dom";
import { RequestGovernanceReview } from "../components/RequestGovernanceReview";
import { Badge, Button, ButtonLink, Card, EmptyState, ErrorState, Notice, PageHeader, Pagination, Spinner, TextInput } from "../components/ui";
import { api } from "../services/api";
import { formatDate } from "../services/format";
import type { DuplicateGroup, RefactorDetail, RefactorFinding, RefactorVerification } from "../types/api";
import { RefactorStatusBadge, SupportBadge } from "./RefactorPage";
import { Confirm, DiffPreview, MutationError } from "./RepairsPage";

type Tone = "success" | "danger" | "warning" | "info" | "neutral";
const OUTCOME_TONE: Record<string, Tone> = { PENDING: "info", PASSED: "success", FAILED: "danger", INCOMPLETE: "warning", BLOCKED: "warning" };
const CRIT_TONE: Record<string, Tone> = { pass: "success", fail: "danger", incomplete: "warning", blocked: "warning", unavailable: "neutral" };
const SEV_TONE: Record<string, Tone> = { blocker: "danger", warning: "warning", info: "info" };
const CHECK_TONE: Record<string, Tone> = { pass: "success", fail: "danger", warn: "warning", info: "info", unavailable: "neutral" };
const APPLIED = ["applied", "verification_failed", "verified"];

function Findings({ findings }: { findings: RefactorFinding[] }) {
  if (findings.length === 0) return <p className="text-sm text-slate-600">No findings.</p>;
  const order = ["blocker", "warning", "info"];
  return (
    <ul className="space-y-1 text-sm" aria-label="Findings">
      {[...findings].sort((a, b) => order.indexOf(a.severity) - order.indexOf(b.severity)).map((f, i) => (
        <li key={`${f.code}-${i}`} className="flex flex-wrap items-start gap-2">
          <Badge tone={SEV_TONE[f.severity] ?? "neutral"}>{f.severity}</Badge><code className="text-xs">{f.code}</code><span>{f.message}</span>
          {f.locations.slice(0, 3).map((l) => <span key={`${l.path}:${l.line}`} className="text-xs text-slate-500">{l.path}:{l.line}</span>)}
        </li>
      ))}
    </ul>
  );
}

function List({ title, items }: { title: string; items?: string[] }) {
  if (!items || items.length === 0) return null;
  return <div><p className="font-semibold">{title}</p><ul className="list-disc pl-5">{items.map((x) => <li key={x}>{x}</li>)}</ul></div>;
}

function ImpactCard({ t }: { t: RefactorDetail }) {
  const i = t.impact;
  if (!i || Object.keys(i).length === 0) return null;
  const tests = i.affected_tests;
  const testFiles = [...new Set(Object.values(tests?.associated ?? {}).flat())];
  return (
    <Card title="Impact">
      <div className="space-y-2 text-sm" data-testid="impact">
        {i.index_complete === false && <Notice tone="warning">This impact view is based on an index that is out of date; dependents and tests may be missing.</Notice>}
        {i.error && <Notice tone="warning">{i.error}</Notice>}
        {(i.caveats ?? []).map((c) => <Notice key={c} tone="info">{c}</Notice>)}
        <p><span className="font-semibold">Direct dependents:</span> {i.direct_dependents?.length ?? 0}{i.dependent_summary && i.dependent_summary.transitive > 0 ? `, transitive: ${i.dependent_summary.transitive}` : ""}</p>
        <List title="Direct dependents" items={i.direct_dependents?.slice(0, 20)} />
        {i.cycles && <p><span className="font-semibold">Dependency cycles:</span> {i.cycles.proof}</p>}
        {(i.cycles?.existing_cycles_involving_changed_files?.length ?? 0) > 0 && <Notice tone="warning">{i.cycles!.existing_cycles_involving_changed_files!.length} existing cycle(s) include changed files (they already existed).</Notice>}
        <p><span className="font-semibold">Affected tests:</span> {tests?.discovered ? (testFiles.length ? testFiles.join(", ") : "none associated") : "test discovery has not been run"}</p>
        {tests?.basis && <p className="text-xs text-slate-500">{tests.basis}</p>}
        <List title="Public entry points in changed files" items={i.public_entry_points?.slice(0, 8).map((e) => `${e.rel_path}${e.symbol ? ` · ${e.symbol}` : ""} (${e.reason})`)} />
      </div>
    </Card>
  );
}

function DuplicateExplorer({ rid, tid }: { rid: string; tid: string }) {
  const [kind, setKind] = useState("");
  const [path, setPath] = useState("");
  const [minSim, setMinSim] = useState("");
  const [page, setPage] = useState(1);
  const q = useQuery({ queryKey: ["refactor-dups", rid, tid, kind, path, minSim, page], queryFn: () => api.refactorDuplicates(rid, tid, { kind, path, min_similarity: minSim ? Number(minSim) : undefined, page }) });
  return (
    <Card title="Duplicate groups">
      <div className="mb-3 grid gap-2 sm:grid-cols-3">
        <select aria-label="Filter by kind" value={kind} onChange={(e) => { setKind(e.target.value); setPage(1); }} className="rounded border border-slate-300 px-2 py-1.5 text-sm">
          <option value="">Any kind</option><option value="exact">exact</option><option value="renamed">renamed</option><option value="structural">structural</option>
        </select>
        <TextInput aria-label="Filter by path" placeholder="Path prefix" value={path} onChange={(e) => { setPath(e.target.value); setPage(1); }} />
        <TextInput aria-label="Minimum similarity" placeholder="Minimum similarity (0–1)" value={minSim} onChange={(e) => { setMinSim(e.target.value); setPage(1); }} />
      </div>
      {q.isLoading ? <Spinner label="Loading groups…" /> : q.isError ? <ErrorState error={q.error} onRetry={() => void q.refetch()} /> : q.data!.groups.length === 0 ? (
        <EmptyState title="No duplicate groups">{kind || path || minSim ? "No group matches these filters." : "Nothing at or above the threshold was found. This does not prove there are no duplicates."}</EmptyState>
      ) : (
        <>
          <p className="mb-2 text-xs text-slate-500">{q.data!.stats.files_scanned} file(s) scanned, {q.data!.stats.units_compared} unit(s) compared. Groups are evidence for review; nothing is merged automatically.</p>
          <ul className="space-y-3">{q.data!.groups.map((g) => <GroupView key={g.id} g={g} />)}</ul>
          <Pagination page={q.data!.page} pages={Math.max(1, Math.ceil(q.data!.total / q.data!.page_size))} total={q.data!.total} onPage={setPage} />
          <ul className="mt-2 list-disc pl-5 text-xs text-slate-500">{q.data!.limitations.map((l) => <li key={l}>{l}</li>)}</ul>
        </>
      )}
    </Card>
  );
}

function GroupView({ g }: { g: DuplicateGroup }) {
  return (
    <li className="rounded border border-slate-200 p-2 text-sm" data-testid="dup-group">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={g.kind === "structural" ? "warning" : "info"}>{g.kind}</Badge><span>similarity {g.similarity.toFixed(2)}</span><span className="text-xs text-slate-500">{g.members.length} locations · {g.lines_total ?? ""}</span>
      </div>
      <p className="text-xs text-slate-600">{g.similarity_meaning}</p>
      <ul className="mt-1">{g.members.map((m) => (
        <li key={`${m.rel_path}:${m.start_line}`}><code>{m.rel_path}:{m.start_line}-{m.end_line}</code> {m.name}
          <details><summary className="cursor-pointer text-xs">Excerpt</summary><pre className="overflow-x-auto bg-slate-50 p-1 text-xs">{m.excerpt}</pre></details></li>))}
      </ul>
      <p className="mt-1"><span className="font-semibold">Differences:</span> {g.differences.summary}</p>
      <p><span className="font-semibold">Strategies:</span> {g.strategies.map((s) => s.summary).join(" Or: ")}</p>
    </li>
  );
}

function Suggestions({ t }: { t: RefactorDetail }) {
  const ex = t.plan.extra;
  if (t.type_id === "python.simplify_conditionals") {
    const list = (ex.suggestions ?? []) as { pattern: string; rel_path: string; start_line: number; before: string; after: string; equivalence: string }[];
    return (
      <Card title="Simplification suggestions (proposal only)">
        {list.length === 0 ? <EmptyState title="Nothing to suggest">No supported pattern was found.</EmptyState> : (
          <ul className="space-y-3">{list.map((s) => (
            <li key={`${s.pattern}:${s.start_line}`} className="text-sm">
              <p><Badge>{s.pattern.replace(/_/g, " ")}</Badge> <code>{s.rel_path}:{s.start_line}</code></p>
              <pre className="overflow-x-auto bg-red-50 p-1 text-xs">{s.before}</pre><pre className="overflow-x-auto bg-green-50 p-1 text-xs">{s.after}</pre>
              <p className="text-xs text-slate-600">Why this is equivalent: {s.equivalence}</p>
            </li>))}</ul>
        )}
      </Card>
    );
  }
  if (t.type_id === "architecture.cycle_suggestions") {
    const list = (ex.cycles ?? []) as { id: string; files: string[]; weakest_edge: { from: string; to: string; imported_names: string[] }; strategies: { id: string; summary: string }[] }[];
    return (
      <Card title="Import cycles (proposal only)">
        {list.length === 0 ? <EmptyState title="No import cycles">None were found in the published index.</EmptyState> : (
          <ul className="space-y-3">{list.map((c) => (
            <li key={c.id} className="text-sm"><p>{c.files.join(" ↔ ")}</p>
              <p className="text-xs">Weakest edge: {c.weakest_edge.from} → {c.weakest_edge.to} ({c.weakest_edge.imported_names.join(", ")})</p>
              <ul className="list-disc pl-5">{c.strategies.map((s) => <li key={s.id}>{s.summary}</li>)}</ul></li>))}</ul>
        )}
      </Card>
    );
  }
  return null;
}

function VerificationPanel({ rid, t }: { rid: string; t: RefactorDetail }) {
  const qc = useQueryClient();
  const [scope, setScope] = useState<"selected" | "all">("selected");
  const [confirm, setConfirm] = useState(false);
  const preview = useQuery({ queryKey: ["refactor-vprev", rid, t.id, scope], queryFn: () => api.previewRefactorVerification(rid, t.id, scope), enabled: confirm });
  const list = useQuery({
    queryKey: ["refactor-verifs", rid, t.id], queryFn: () => api.refactorVerifications(rid, t.id),
    refetchInterval: (q) => (q.state.data?.some((v) => v.outcome === "PENDING") ? 1500 : false),
  });
  const start = useMutation({
    mutationFn: () => api.startRefactorVerification(rid, t.id, scope),
    onSuccess: () => { setConfirm(false); void qc.invalidateQueries({ queryKey: ["refactor-verifs"] }); void qc.invalidateQueries({ queryKey: ["refactor", rid, t.id] }); },
  });
  const pending = list.data?.some((v) => v.outcome === "PENDING");
  return (
    <Card title="Verification">
      <Notice tone="info">Verification runs the repository's own discovered tests through the controlled runner and compares them with a baseline. Passing tests are evidence, not proof that behavior is unchanged.</Notice>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <label className="text-sm">Scope <select aria-label="Verification scope" value={scope} onChange={(e) => setScope(e.target.value as "selected" | "all")} className="rounded border border-slate-300 px-2 py-1 text-sm">
          <option value="selected">Tests linked to the changed files</option><option value="all">All discovered tests</option></select></label>
        <Button onClick={() => setConfirm(true)} disabled={Boolean(pending) || start.isPending}>Review and run verification</Button>
      </div>
      {confirm && (
        <div className="mt-2">
          {preview.isLoading ? <Spinner label="Selecting tests…" /> : preview.isError ? <ErrorState error={preview.error} /> : preview.data && (
            <Confirm title="Run tests now?" confirmLabel="Run tests" onConfirm={() => start.mutate()} onCancel={() => setConfirm(false)} busy={start.isPending}>
              <p>This executes the repository's tests on this machine. {preview.data.execution}</p>
              <ul className="list-disc pl-5">{preview.data.selected.length === 0 ? <li>No test is linked to the changed files.</li> : preview.data.selected.map((s) => <li key={s.target}>{s.target} — {s.reason}</li>)}</ul>
              <p>Baseline: {preview.data.baseline_execution_run_id ? preview.data.baseline_note : preview.data.baseline_note || "none"}</p>
              <p className="text-xs">{preview.data.not_proven}</p>
            </Confirm>
          )}
        </div>
      )}
      {start.error && <MutationError error={start.error} />}
      {list.isLoading ? <Spinner label="Loading results…" /> : list.isError ? <ErrorState error={list.error} onRetry={() => void list.refetch()} /> : list.data!.length === 0 ? (
        <p className="mt-2 text-sm text-slate-600">Not verified yet. Applying a change does not verify it.</p>
      ) : <ul className="mt-3 space-y-3">{list.data!.map((v) => <VerifView key={v.id} v={v} />)}</ul>}
    </Card>
  );
}

function VerifView({ v }: { v: RefactorVerification }) {
  return (
    <li className="rounded border border-slate-200 p-2 text-sm" data-testid="verification">
      <div className="flex flex-wrap items-center gap-2"><Badge tone={OUTCOME_TONE[v.outcome] ?? "neutral"}>{v.outcome}</Badge><span>{formatDate(v.created_at)}</span></div>
      <p>{v.outcome === "PENDING" ? "The test run is in progress." : v.outcome_reason}</p>
      <p className="text-xs text-slate-600">{v.meaning}</p>
      <ul className="mt-1 space-y-0.5">{v.criteria.map((c) => <li key={c.id}><Badge tone={CRIT_TONE[c.status] ?? "neutral"}>{c.status}</Badge> <code className="text-xs">{c.id}</code> {c.detail}</li>)}</ul>
      {v.comparison.baseline_available === false && <p className="text-xs text-slate-600">{v.comparison.note}</p>}
      {v.comparison.baseline_note && <p className="text-xs text-slate-600">{v.comparison.baseline_note}</p>}
      {v.run && <details className="mt-1"><summary className="cursor-pointer text-xs">Run: {v.run.status}, exit {String(v.run.exit_code)} · passed {v.run.tests_passed ?? 0}, failed {v.run.tests_failed ?? 0}</summary>
        <pre className="overflow-x-auto bg-slate-50 p-1 text-xs">{v.run.stdout_tail || "(no output)"}</pre></details>}
    </li>
  );
}

export function RefactorDetailPage() {
  const { repositoryId = "", refactorId = "" } = useParams();
  const qc = useQueryClient();
  const [ack, setAck] = useState<string[]>([]);
  const [reason, setReason] = useState("");
  const [open, setOpen] = useState<"" | "approve" | "reject" | "apply" | "baseline">("");
  const key = ["refactor", repositoryId, refactorId];
  const q = useQuery({ queryKey: key, queryFn: () => api.refactor(repositoryId, refactorId) });
  const t = q.data;
  const diff = useQuery({ queryKey: ["refactor-diff", repositoryId, refactorId, t?.status], queryFn: () => api.refactorDiff(repositoryId, refactorId), enabled: Boolean(t?.has_diff) });
  const audit = useQuery({ queryKey: ["refactor-audit", repositoryId, refactorId, t?.status], queryFn: () => api.refactorAudit(repositoryId, refactorId), enabled: Boolean(t) });
  const rollback = useQuery({ queryKey: ["refactor-rollback", repositoryId, refactorId], queryFn: () => api.refactorRollback(repositoryId, refactorId), enabled: Boolean(t && APPLIED.includes(t.status)) });
  const act = useMutation({
    mutationFn: async (a: string) => {
      if (a === "validate") return api.validateRefactor(repositoryId, refactorId);
      if (a === "submit") return api.submitRefactor(repositoryId, refactorId);
      if (a === "approve") return api.approveRefactor(repositoryId, refactorId, ack);
      if (a === "reject") return api.rejectRefactor(repositoryId, refactorId, reason.trim());
      if (a === "close") return api.closeRefactor(repositoryId, refactorId);
      if (a === "baseline") { await api.refactorBaseline(repositoryId, refactorId); return api.refactor(repositoryId, refactorId); }
      return api.applyRefactor(repositoryId, refactorId);
    },
    onSuccess: () => { setOpen(""); void qc.invalidateQueries({ queryKey: ["refactor"] }); void qc.invalidateQueries({ queryKey: ["refactors"] }); void qc.invalidateQueries({ queryKey: ["freshness"] }); },
  });
  if (q.isLoading) return <Spinner label="Loading refactoring…" />;
  if (q.isError || !t) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  const required = t.plan.acknowledgements_required ?? [];
  const pre = ["generated", "invalid", "validated", "awaiting_approval"].includes(t.status);
  const files = t.files.length;
  const can = (s: string[]) => s.includes(t.status);
  const busy = act.isPending;
  const base = `/repositories/${repositoryId}/refactors`;
  const rows: ReactNode[] = [];
  if (t.validation.checks?.length) {
    rows.push(<ul key="checks" className="space-y-0.5 text-sm" aria-label="Validation checks">{t.validation.checks.map((c) => (
      <li key={c.id}><Badge tone={CHECK_TONE[c.status] ?? "neutral"}>{c.status}</Badge> <code className="text-xs">{c.id}</code> {c.detail}</li>))}</ul>);
  }
  return (
    <>
      <PageHeader title={t.title} subtitle={`${t.type_id} · created ${formatDate(t.created_at)}`} actions={<><ButtonLink to={base} variant="secondary">All refactorings</ButtonLink><RequestGovernanceReview resourceType="refactor" resourceId={t.id} /></>} />
      <div className="flex flex-wrap items-center gap-2"><RefactorStatusBadge status={t.status} /><SupportBadge level={t.support_level} />
        {t.status === "verified" && t.verification_summary.outcome === "PASSED" && <span className="text-xs text-slate-600">Selected tests passed; this is evidence, not proof of unchanged behavior.</span>}
        {t.status === "applied" && <span className="text-xs text-amber-700">Applied to the working tree. Not committed. Not verified.</span>}
      </div>
      {t.plan.snapshot && !t.plan.snapshot.complete && <Notice tone="warning">This plan was made against an incomplete or stale index (state: {t.plan.snapshot.index_state}). Treat dependents and tests as possibly incomplete.</Notice>}
      {act.error && <MutationError error={act.error} />}
      <Card title="Plan">
        <div className="space-y-2 text-sm" data-testid="plan">
          {Object.keys(t.plan.target ?? {}).length > 0 && <p><span className="font-semibold">Target:</span> {Object.entries(t.plan.target).filter(([, v]) => typeof v === "string" || typeof v === "number").map(([k, v]) => `${k}: ${String(v)}`).join(" · ")}</p>}
          <p><span className="font-semibold">Files:</span> {files} · <span className="font-semibold">Edits:</span> {t.plan.edit_count}</p>
          <Findings findings={t.plan.findings} />
          <List title="Side effects" items={t.plan.side_effects} />
          <List title="Limitations" items={t.plan.limitations} />
          {t.plan.verification_strategy && <p className="text-xs text-slate-600">Verification would prove: {t.plan.verification_strategy.proves}. It would not prove: {t.plan.verification_strategy.does_not_prove}.</p>}
        </div>
      </Card>
      <ImpactCard t={t} />
      {t.has_diff && (
        <Card title="Multi-file review">
          <ul className="mb-2 text-sm">{t.files.map((f) => <li key={f.path}><code>{f.path}</code> +{f.added} −{f.removed}</li>)}</ul>
          {diff.isLoading ? <Spinner label="Loading diff…" /> : diff.isError ? <ErrorState error={diff.error} onRetry={() => void diff.refetch()} /> : diff.data && <DiffPreview preview={diff.data.preview} />}
          <details className="mt-2"><summary className="cursor-pointer text-sm">Individual edits ({t.plan.edit_count})</summary>
            <ul className="mt-1 max-h-72 overflow-auto text-xs">{t.plan.edits.map((e, i) => (
              <li key={e.id ?? i}><code>{e.rel_path}:{e.line ?? e.start_line}</code> <Badge>{e.kind}</Badge> {e.confidence === "inferred" && <Badge tone="warning">inferred</Badge>} <code>{e.old.length > 60 ? "…" : e.old}</code> → <code>{e.new.length > 60 ? "…" : e.new}</code></li>))}</ul></details>
        </Card>
      )}
      {t.status === "blocked" && t.plan.candidate_edits.length > 0 && (
        <Card title="Candidate edits (not applicable)"><p className="mb-1 text-sm text-slate-600">These would be changed if the blockers were resolved. They cannot be applied.</p>
          <ul className="max-h-48 overflow-auto text-xs">{t.plan.candidate_edits.slice(0, 50).map((e, i) => <li key={i}><code>{e.rel_path}:{e.line}</code> {e.kind}</li>)}</ul></Card>
      )}
      {t.type_id === "analysis.duplicates" && <DuplicateExplorer rid={repositoryId} tid={refactorId} />}
      <Suggestions t={t} />
      {(t.validation.checks || t.validation.errors?.length) && (
        <Card title="Validation">
          {t.validation.stale && <Notice tone="danger">A source file changed since this plan was made. Create a new plan.</Notice>}
          {(t.validation.errors ?? []).map((e) => <Notice key={e.code + e.message} tone="danger">{e.message}</Notice>)}
          {rows}
          {(t.validation.not_checked ?? []).length > 0 && <List title="Not checked" items={t.validation.not_checked} />}
        </Card>
      )}
      <Card title="Decision">
        <div className="space-y-2">
          <div className="flex flex-wrap gap-2">
            {pre && <Button variant="secondary" onClick={() => act.mutate("validate")} disabled={busy}>{t.status === "generated" ? "Validate" : "Revalidate"}</Button>}
            {t.status === "validated" && <Button onClick={() => act.mutate("submit")} disabled={busy}>Submit for approval</Button>}
            {t.status === "awaiting_approval" && <Button onClick={() => setOpen("approve")} disabled={busy}>Approve…</Button>}
            {t.status === "approved" && <Button variant="secondary" onClick={() => setOpen("baseline")} disabled={busy}>Record baseline tests…</Button>}
            {t.status === "approved" && <Button onClick={() => setOpen("apply")} disabled={busy}>Apply to working tree…</Button>}
            {can(["generated", "invalid", "validated", "awaiting_approval", "approved"]) && <Button variant="secondary" onClick={() => setOpen("reject")} disabled={busy}>Reject…</Button>}
            {can(["blocked", "analyzed"]) && <Button variant="secondary" onClick={() => act.mutate("close")} disabled={busy}>Close</Button>}
          </div>
          {t.status === "blocked" && <Notice tone="danger">This request is blocked and cannot be applied. Resolve the blockers above and create a new plan.</Notice>}
          {t.status === "analyzed" && <Notice tone="info">Analysis only. This request never changes files.</Notice>}
          {open === "approve" && (
            <Confirm title="Approve this refactoring?" confirmLabel="Approve" onConfirm={() => act.mutate("approve")} onCancel={() => setOpen("")} busy={busy} disabled={required.some((r) => !ack.includes(r))}>
              <p>Approval does not change any file. Applying is a separate step.</p>
              {required.length > 0 && <fieldset><legend className="font-semibold">Acknowledge these warnings</legend>{required.map((r) => {
                const f = t.plan.findings.find((x) => x.code === r);
                return <label key={r} className="block"><input type="checkbox" checked={ack.includes(r)} onChange={(e) => setAck(e.target.checked ? [...ack, r] : ack.filter((x) => x !== r))} /> <code>{r}</code> {f?.message}</label>;
              })}</fieldset>}
            </Confirm>
          )}
          {open === "reject" && (
            <Confirm title="Reject this refactoring?" confirmLabel="Reject" onConfirm={() => act.mutate("reject")} onCancel={() => setOpen("")} busy={busy} disabled={reason.trim().length < 5}>
              <label className="block text-sm">Reason <TextInput aria-label="Rejection reason" value={reason} onChange={(e) => setReason(e.target.value)} /></label>
            </Confirm>
          )}
          {open === "apply" && (
            <Confirm title="Apply to the working tree?" confirmLabel="Apply changes" onConfirm={() => act.mutate("apply")} onCancel={() => setOpen("")} busy={busy}>
              <p>This writes {files} file(s) in your repository. Nothing is committed. Originals are retained by NOVA and a reverse diff is available afterwards.</p>
              <p>The files must still match what was validated, or nothing is written.</p>
            </Confirm>
          )}
          {open === "baseline" && (
            <Confirm title="Run baseline tests now?" confirmLabel="Run baseline" onConfirm={() => act.mutate("baseline")} onCancel={() => setOpen("")} busy={busy}>
              <p>Runs the tests linked to the changed files on the unmodified code so later failures can be classed as new or pre-existing. It executes the repository's tests on this machine.</p>
            </Confirm>
          )}
          {t.baseline_execution_run_id && <p className="text-sm text-slate-600">Baseline run recorded ({t.baseline_note}).</p>}
          {t.reject_reason && <p className="text-sm">Rejected: {t.reject_reason}</p>}
        </div>
      </Card>
      {APPLIED.includes(t.status) && (
        <Card title="Applied result and recovery">
          <p className="text-sm">Applied {t.applied_at ? formatDate(t.applied_at) : ""}. The files were written to your working tree; nothing was committed.{" "}
            {t.apply_result.reindex?.scheduled ? "An incremental re-index was started." : t.apply_result.reindex ? `Re-index not started: ${t.apply_result.reindex.reason}` : ""}</p>
          {rollback.data && <details className="mt-1"><summary className="cursor-pointer text-sm">Undo guidance and reverse diff</summary>
            <ul className="list-disc pl-5 text-sm">{rollback.data.instructions.map((s) => <li key={s}>{s}</li>)}</ul>
            <pre tabIndex={0} aria-label="Reverse diff" className="max-h-60 overflow-auto bg-slate-50 p-1 text-xs">{rollback.data.reverse_diff}</pre></details>}
        </Card>
      )}
      {APPLIED.includes(t.status) && <VerificationPanel rid={repositoryId} t={t} />}
      <Card title="Audit trail">
        {audit.isLoading ? <Spinner label="Loading audit…" /> : audit.isError ? <ErrorState error={audit.error} /> : (
          <ul className="text-sm" aria-label="Audit trail">{audit.data!.items.map((a) => <li key={a.id}><code>{a.action}</code> <span className="text-xs text-slate-500">{formatDate(a.created_at)}</span></li>)}</ul>
        )}
      </Card>
    </>
  );
}
