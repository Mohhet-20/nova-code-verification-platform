import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Badge, Button, ButtonLink, Card, EmptyState, ErrorState, Field, Notice, PageHeader, Pagination, Spinner, TextArea, TextInput } from "../components/ui";
import { ApiError, api, describeError } from "../services/api";
import { formatDate } from "../services/format";
import type { PatchDetail, PatchItem, PatchStatus, RepairStatus, RepairVerification, RepairOutcome } from "../types/api";

type Tone = "success" | "danger" | "warning" | "info" | "neutral";
const STATUS_TONE: Record<RepairStatus, Tone> = {
  open: "neutral", analyzed: "info", proposed: "info", patched: "info", applied: "warning", verified: "success", verification_failed: "danger", closed: "neutral",
};
const STATUS_LABEL: Record<RepairStatus, string> = {
  open: "Open", analyzed: "Evidence assembled", proposed: "Proposal ready", patched: "Patch drafted", applied: "Applied, NOT verified", verified: "Verified",
  verification_failed: "Verification failed", closed: "Closed",
};
const PATCH_TONE: Record<PatchStatus, Tone> = {
  draft: "neutral", validated: "info", invalid: "danger", awaiting_approval: "warning", approved: "info", rejected: "neutral", applying: "warning",
  applied: "warning", verification_failed: "danger", verified: "success",
};
const PATCH_LABEL: Record<PatchStatus, string> = {
  draft: "Draft", validated: "Validated", invalid: "Invalid", awaiting_approval: "Awaiting approval", approved: "Approved (not applied)", rejected: "Rejected",
  applying: "Applying", applied: "Applied, not verified", verification_failed: "Verification failed", verified: "Verified",
};
const OUTCOME_TONE: Record<RepairOutcome, Tone> = { PENDING: "info", PASSED: "success", FAILED: "danger", INCOMPLETE: "warning", BLOCKED: "warning" };
const SEV_TONE: Record<string, Tone> = { critical: "danger", high: "danger", medium: "warning", low: "info", info: "neutral" };
const CONF_TONE: Record<string, Tone> = { high: "success", medium: "warning", low: "neutral" };
const SEVERITIES = ["critical", "high", "medium", "low", "info"];
const STATUSES = Object.keys(STATUS_LABEL) as RepairStatus[];
const OUTCOMES: RepairOutcome[] = ["PENDING", "PASSED", "FAILED", "INCOMPLETE", "BLOCKED"];

export const RepairStatusBadge = ({ status }: { status: RepairStatus }) => <Badge tone={STATUS_TONE[status]}>{STATUS_LABEL[status]}</Badge>;
export const PatchStatusBadge = ({ status }: { status: PatchStatus }) => <Badge tone={PATCH_TONE[status]}>{PATCH_LABEL[status]}</Badge>;

export function MutationError({ error }: { error: unknown }) {
  if (error instanceof ApiError && error.status === 403) return <Notice tone="warning">Your role does not permit this action. You can still read results.</Notice>;
  return <Notice tone="danger"><span data-testid="mutation-error">{describeError(error)}</span></Notice>;
}

/** Inline confirmation for actions that change files or decisions. Never uses a browser dialog. */
export function Confirm({ title, children, confirmLabel, onConfirm, onCancel, busy, disabled }: {
  title: string; children: ReactNode; confirmLabel: string; onConfirm: () => void; onCancel: () => void; busy?: boolean; disabled?: boolean;
}) {
  return (
    <div role="alertdialog" aria-label={title} className="rounded border-2 border-amber-400 bg-amber-50 p-3 text-sm">
      <p className="font-semibold">{title}</p>
      <div className="mt-1 space-y-1">{children}</div>
      <div className="mt-2 flex gap-2">
        <Button onClick={onConfirm} disabled={busy || disabled}>{busy ? "Working…" : confirmLabel}</Button>
        <Button variant="secondary" onClick={onCancel} disabled={busy}>Cancel</Button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ list + creation

export function RepairsPage() {
  const { repositoryId = "" } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [page, setPage] = useState(1);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [severity, setSeverity] = useState("");
  const [outcome, setOutcome] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [sev, setSev] = useState("medium");
  const [failureId, setFailureId] = useState("");
  const [hints, setHints] = useState("");
  const repo = useQuery({ queryKey: ["repository", repositoryId], queryFn: () => api.getRepository(repositoryId) });
  const list = useQuery({
    queryKey: ["repairs", repositoryId, page, q, status, severity, outcome],
    queryFn: () => api.repairs(repositoryId, { page, q, status, severity, verification_outcome: outcome }),
  });
  const failures = useQuery({ queryKey: ["failures", repositoryId, "open-for-repair"], queryFn: () => api.failures(repositoryId, {}) });
  const create = useMutation({
    mutationFn: () => api.createRepair(repositoryId, {
      title: title.trim(), description: description.trim(), severity: sev, failure_id: failureId || undefined,
      hint_files: hints.split("\n").map((h) => h.trim()).filter(Boolean),
    }),
    onSuccess: (r) => { void qc.invalidateQueries({ queryKey: ["repairs"] }); navigate(`/repositories/${repositoryId}/repairs/${r.id}`); },
  });
  const filtered = Boolean(q || status || severity || outcome);
  const reset = (fn: (v: string) => void) => (e: { target: { value: string } }) => { fn(e.target.value); setPage(1); };
  return (
    <>
      <PageHeader
        title="Repair intelligence"
        subtitle={repo.data ? `Evidence-based repair suggestions for ${repo.data.name}. A suggested fix is only "verified" after the selected tests have actually passed.` : undefined}
        actions={<ButtonLink to={`/repositories/${repositoryId}`} variant="secondary">Back to repository</ButtonLink>}
      />
      <Card title="Start an investigation">
        <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); if (title.trim()) create.mutate(); }}>
          <Notice tone="info">
            Investigations assemble evidence from your recorded failures and findings, rank likely locations with documented rules, and can draft a small deterministic patch.
            Nothing is changed in your repository until you approve and apply a patch yourself. No AI provider is used.
          </Notice>
          <Field label="Title" htmlFor="rp-title"><TextInput id="rp-title" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} /></Field>
          <Field label="What is going wrong?" htmlFor="rp-desc"><TextArea id="rp-desc" rows={3} value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Severity" htmlFor="rp-sev">
              <select id="rp-sev" value={sev} onChange={(e) => setSev(e.target.value)} className="rounded border border-slate-300 px-2 py-1.5 text-sm">
                {SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </Field>
            <Field label="Linked failure (optional)" htmlFor="rp-failure" hint="Recorded failures give the strongest evidence.">
              <select id="rp-failure" value={failureId} onChange={(e) => setFailureId(e.target.value)} className="rounded border border-slate-300 px-2 py-1.5 text-sm">
                <option value="">None</option>
                {failures.data?.items.map((f) => <option key={f.id} value={f.id}>{(f.test_id ?? f.summary).slice(0, 80)}</option>)}
              </select>
            </Field>
          </div>
          <Field label="File hints (optional)" htmlFor="rp-hints" hint="One repository-relative path per line. Hints are labelled as stated by you, not observed.">
            <TextArea id="rp-hints" rows={2} value={hints} onChange={(e) => setHints(e.target.value)} />
          </Field>
          <Button onClick={() => title.trim() && create.mutate()} disabled={create.isPending || !title.trim()}>{create.isPending ? "Creating…" : "Create investigation"}</Button>
          {create.error && <MutationError error={create.error} />}
        </form>
      </Card>
      <Card title="Investigations">
        <div className="mb-3 grid gap-2 sm:grid-cols-4">
          <TextInput aria-label="Search investigations" placeholder="Search title…" value={q} onChange={reset(setQ)} />
          <select aria-label="Filter by status" value={status} onChange={reset(setStatus)} className="rounded border border-slate-300 px-2 py-1.5 text-sm">
            <option value="">Any status</option>{STATUSES.map((s) => <option key={s} value={s}>{STATUS_LABEL[s]}</option>)}
          </select>
          <select aria-label="Filter by severity" value={severity} onChange={reset(setSeverity)} className="rounded border border-slate-300 px-2 py-1.5 text-sm">
            <option value="">Any severity</option>{SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <select aria-label="Filter by verification outcome" value={outcome} onChange={reset(setOutcome)} className="rounded border border-slate-300 px-2 py-1.5 text-sm">
            <option value="">Any verification outcome</option>{OUTCOMES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
        {list.isLoading ? <Spinner label="Loading investigations…" /> : list.isError ? <ErrorState error={list.error} onRetry={() => void list.refetch()} /> : list.data!.items.length === 0 ? (
          <EmptyState title={filtered ? "No investigations match these filters" : "No investigations yet"}>
            {filtered ? "Clear a filter to see more." : "Create one above, or start from a recorded failure."}
          </EmptyState>
        ) : (
          <>
            <ul className="divide-y divide-slate-100">
              {list.data!.items.map((r) => (
                <li key={r.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                  <div>
                    <Link className="font-medium text-indigo-700 hover:underline" to={`/repositories/${repositoryId}/repairs/${r.id}`}>{r.title}</Link>
                    <div className="text-xs text-slate-500">
                      {formatDate(r.created_at)} · {r.source_kind} · {r.patch_count} patch(es){r.root_cause.rel_path ? ` · likely ${r.root_cause.rel_path}` : ""}
                    </div>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone={SEV_TONE[r.severity] ?? "neutral"}>{r.severity}</Badge>
                    {r.verification_outcome && <Badge tone={OUTCOME_TONE[r.verification_outcome]}>verification {r.verification_outcome}</Badge>}
                    <RepairStatusBadge status={r.status} />
                  </div>
                </li>
              ))}
            </ul>
            <Pagination page={list.data!.page} pages={list.data!.pages} total={list.data!.total} onPage={setPage} />
          </>
        )}
      </Card>
    </>
  );
}

// ------------------------------------------------------------------------------------------------ detail

function List({ items, empty }: { items?: string[]; empty: string }) {
  return items && items.length ? <ul className="list-disc space-y-0.5 pl-5 text-sm">{items.map((i) => <li key={i}>{i}</li>)}</ul> : <p className="text-sm text-slate-500">{empty}</p>;
}

function Evidence({ repositoryId, id }: { repositoryId: string; id: string }) {
  const qc = useQueryClient();
  const d = useQuery({ queryKey: ["repair", repositoryId, id], queryFn: () => api.repair(repositoryId, id) });
  const refresh = useMutation({
    mutationFn: async () => { await api.repairEvidence(repositoryId, id); return api.repairProposal(repositoryId, id); },
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["repair", repositoryId, id] }),
  });
  if (!d.data) return null;
  const ev = d.data.evidence;
  const pr = d.data.proposal;
  const assembled = Boolean(d.data.evidence_assembled_at);
  const rc = d.data.root_cause;
  return (
    <Card title="Evidence and root cause" actions={<Button variant="secondary" onClick={() => refresh.mutate()} disabled={refresh.isPending}>{refresh.isPending ? "Working…" : assembled ? "Refresh evidence and proposal" : "Assemble evidence and proposal"}</Button>}>
      {refresh.error && <MutationError error={refresh.error} />}
      {!assembled ? <p className="text-sm text-slate-600">No evidence has been assembled yet.</p> : (
        <div className="space-y-4">
          {ev.state === "insufficient_evidence" && <Notice tone="warning">Insufficient evidence: the available data does not identify a likely location. No patch will be suggested.</Notice>}
          {rc.summary && (
            <div className="rounded border border-slate-200 p-3 text-sm" data-testid="root-cause">
              <div className="flex flex-wrap items-center gap-2"><span className="font-semibold">Root-cause hypothesis</span>
                {rc.confidence && <Badge tone={CONF_TONE[rc.confidence] ?? "neutral"}>{rc.confidence} confidence</Badge>}<Badge tone="neutral">unconfirmed</Badge></div>
              <p>{rc.summary}</p>
              {rc.confidence_rationale && <p className="text-xs text-slate-600">Why: {rc.confidence_rationale}</p>}
              {rc.note && <p className="text-xs text-slate-500">{rc.note}</p>}
            </div>
          )}
          <section aria-label="Facts">
            <h3 className="text-sm font-semibold">Facts <span className="font-normal text-slate-500">(with their source)</span></h3>
            <ul className="space-y-1 text-sm">{(ev.facts ?? []).map((f) => (
              <li key={f.id}><Badge tone={f.basis === "observed" ? "success" : "neutral"}>{f.basis === "observed" ? "observed" : "stated by requester"}</Badge> {f.text} <span className="text-xs text-slate-500">[{f.source}]</span></li>
            ))}</ul>
            {!(ev.facts ?? []).length && <p className="text-sm text-slate-500">No facts were recorded.</p>}
          </section>
          <section aria-label="Hypotheses">
            <h3 className="text-sm font-semibold">Hypotheses <span className="font-normal text-slate-500">(ranked guesses, not facts)</span></h3>
            {(ev.hypotheses ?? []).length === 0 ? <p className="text-sm text-slate-500">No candidate locations were found.</p> : (
              <ol className="space-y-2">{ev.hypotheses!.map((h) => (
                <li key={`${h.rel_path}:${h.symbol}`} className="rounded border border-slate-200 p-2 text-sm">
                  <div className="flex flex-wrap items-center gap-2"><span className="font-mono">#{h.rank} {h.rel_path}{h.symbol ? ` · ${h.symbol}` : ""}</span>
                    <Badge tone={CONF_TONE[h.confidence] ?? "neutral"}>{h.confidence}</Badge><span className="text-xs text-slate-500">score {h.score}</span></div>
                  <p className="text-xs text-slate-600">{h.rationale}</p>
                  <ul className="mt-1 text-xs text-slate-600">{h.evidence.map((e) => <li key={e.id}>+{e.weight} {e.detail}</li>)}</ul>
                </li>
              ))}</ol>
            )}
          </section>
          <div className="grid gap-4 sm:grid-cols-2">
            <section aria-label="Unknowns"><h3 className="text-sm font-semibold">Unknowns</h3><List items={ev.unknowns} empty="None recorded." /></section>
            <section aria-label="Next steps"><h3 className="text-sm font-semibold">Diagnostic next steps</h3><List items={ev.next_steps} empty="None." /></section>
          </div>
          <details className="text-sm"><summary className="cursor-pointer font-medium">Ranking method and limitations</summary>
            <p className="mt-1 text-xs text-slate-600">Weights: {Object.entries(ev.weights ?? {}).map(([k, v]) => `${k}=${v}`).join(", ")}. Minimum score for a hypothesis: {ev.min_score}.</p>
            <List items={ev.limitations} empty="" /></details>
          {pr.state && (
            <section aria-label="Proposal" className="rounded border border-slate-200 p-3 text-sm" data-testid="proposal">
              <div className="flex items-center gap-2"><h3 className="font-semibold">Repair proposal</h3>
                <Badge tone={pr.state === "ready" ? "success" : "warning"}>{pr.state.replace(/_/g, " ")}</Badge><Badge tone="neutral">deterministic, no AI</Badge></div>
              {pr.strategy && <p><span className="font-medium">Strategy:</span> {pr.strategy}</p>}
              {pr.expected_behavior && <p><span className="font-medium">Expected behavior:</span> {pr.expected_behavior}</p>}
              {pr.patch_reason && <p className="text-xs text-slate-600">{pr.patch_reason}</p>}
              {!pr.patch_available && <Notice tone="warning">No safe automatic patch is available. Use the diagnostic steps above, or supply your own patch below.</Notice>}
              {(pr.side_effects ?? []).length > 0 && <><p className="mt-1 font-medium">Possible side effects</p><List items={pr.side_effects} empty="" /></>}
              {(pr.tests_to_run ?? []).length > 0 && <><p className="mt-1 font-medium">Tests to run</p>
                <ul className="list-disc pl-5 text-xs">{pr.tests_to_run!.map((t) => <li key={t.target}><span className="font-mono">{t.target}</span> — {t.reason}</li>)}</ul></>}
            </section>
          )}
        </div>
      )}
    </Card>
  );
}

function DiffView({ patch }: { patch: PatchDetail }) {
  return <DiffPreview preview={patch.preview} />;
}

export function DiffPreview({ preview }: { preview: Pick<PatchDetail["preview"], "files" | "truncated"> }) {
  const files = preview.files ?? [];
  return (
    <div className="space-y-3" data-testid="diff-view">
      {files.map((f) => (
        <div key={f.path} className="overflow-hidden rounded border border-slate-200">
          <div className="bg-slate-100 px-2 py-1 font-mono text-xs">{f.change_type} · {f.path} (+{f.added} −{f.removed})</div>
          {f.hunks.map((h) => (
            <pre key={h.header} tabIndex={0} aria-label={`Changes in ${f.path} ${h.header}`} className="overflow-x-auto text-xs leading-5">
              <div className="bg-slate-50 px-2 text-slate-500">{h.header}</div>
              {h.lines.map((l, i) => (
                <div key={i} className={l.kind === "add" ? "bg-green-50 px-2 text-green-800" : l.kind === "remove" ? "bg-red-50 px-2 text-red-800" : "px-2"}>
                  {l.kind === "add" ? "+" : l.kind === "remove" ? "-" : " "}{l.text}
                </div>
              ))}
            </pre>
          ))}
        </div>
      ))}
      {preview.truncated && <Notice tone="warning">The preview is truncated. The stored patch is complete.</Notice>}
    </div>
  );
}

type Pending = null | "approve" | "reject" | "apply";

function PatchPanel({ repositoryId, repairId, summary }: { repositoryId: string; repairId: string; summary: PatchItem }) {
  const qc = useQueryClient();
  const [pending, setPending] = useState<Pending>(null);
  const [reason, setReason] = useState("");
  const [scope, setScope] = useState<"selected" | "all">("selected");
  const [verifyAsk, setVerifyAsk] = useState(false);
  const detail = useQuery({ queryKey: ["repair-patch", repositoryId, repairId, summary.id], queryFn: () => api.repairPatch(repositoryId, repairId, summary.id) });
  const verifs = useQuery({
    queryKey: ["repair-verifs", repositoryId, repairId, summary.id], queryFn: () => api.repairVerifications(repositoryId, repairId, summary.id),
    refetchInterval: (q) => (q.state.data?.some((v) => v.outcome === "PENDING") ? 1500 : false),
  });
  const refresh = () => { void qc.invalidateQueries({ queryKey: ["repair"] }); void qc.invalidateQueries({ queryKey: ["repair-patch"] }); void qc.invalidateQueries({ queryKey: ["repair-verifs"] }); void qc.invalidateQueries({ queryKey: ["repairs"] }); };
  const act = useMutation({
    mutationFn: async (a: "validate" | "submit" | "approve" | "reject" | "apply") => {
      if (a === "reject") return api.rejectPatch(repositoryId, repairId, summary.id, reason.trim());
      if (a === "apply") return api.applyPatch(repositoryId, repairId, summary.id);
      return api.patchAction(repositoryId, repairId, summary.id, a);
    },
    onSuccess: () => { setPending(null); setReason(""); refresh(); },
    onError: () => { setPending(null); refresh(); },
  });
  const vprev = useQuery({ queryKey: ["repair-vprev", repositoryId, repairId, summary.id, scope], queryFn: () => api.previewRepairVerification(repositoryId, repairId, summary.id, scope), enabled: verifyAsk });
  const start = useMutation({ mutationFn: () => api.startRepairVerification(repositoryId, repairId, summary.id, scope), onSuccess: () => { setVerifyAsk(false); refresh(); } });
  const rollback = useQuery({ queryKey: ["repair-rb", repositoryId, repairId, summary.id], queryFn: () => api.patchRollback(repositoryId, repairId, summary.id), enabled: ["applied", "verification_failed", "verified"].includes(summary.status) });
  const p = detail.data;
  const s = p?.status ?? summary.status;
  const val = p?.validation ?? summary.validation;
  return (
    <Card title={<span className="flex flex-wrap items-center gap-2">{summary.title} <PatchStatusBadge status={s} /></span>}>
      <div className="space-y-3 text-sm">
        {summary.source === "ai_assisted" && <Badge tone="warning">AI-assisted · human review required</Badge>}
        <p className="text-xs text-slate-500">{summary.source === "generated" ? `Generated by rule ${summary.rule_id}` : summary.source === "ai_assisted" ? "Proposed by Claude, checked by NOVA" : "Supplied manually"} · sha256 {summary.patch_sha256.slice(0, 12)}… · created {formatDate(summary.created_at)}</p>
        <p>{summary.summary}</p>
        {s === "invalid" && <Notice tone="danger">This patch is no longer valid for the current files (it may be stale). It was not applied.</Notice>}
        {val.stale && <Notice tone="warning">Stale: a file changed after this patch was created.</Notice>}
        {(val.errors ?? []).map((e) => <Notice key={e.code + e.message} tone="danger">{e.code}: {e.message}</Notice>)}
        {(val.warnings ?? []).map((w) => <Notice key={w} tone="warning">{w}</Notice>)}
        {val.ok && <p className="text-xs text-green-700">Validated in a temporary copy. Your working tree was not modified.
          {val.static_check?.available && ` Static re-check: ${val.static_check.new_findings.length} new finding(s), ${val.static_check.resolved_findings} resolved.`}</p>}
        {detail.isLoading ? <Spinner label="Loading diff…" /> : p ? <DiffView patch={p} /> : null}
        <div className="flex flex-wrap gap-2">
          {["draft", "invalid", "validated"].includes(s) && <Button variant="secondary" onClick={() => act.mutate("validate")} disabled={act.isPending}>Validate</Button>}
          {s === "validated" && <Button variant="secondary" onClick={() => act.mutate("submit")} disabled={act.isPending}>Submit for approval</Button>}
          {s === "awaiting_approval" && <Button onClick={() => setPending("approve")} disabled={act.isPending}>Approve…</Button>}
          {["validated", "awaiting_approval", "approved"].includes(s) && <Button variant="secondary" onClick={() => setPending("reject")} disabled={act.isPending}>Reject…</Button>}
          {s === "approved" && <Button onClick={() => setPending("apply")} disabled={act.isPending}>Apply to working tree…</Button>}
          {["applied", "verification_failed"].includes(s) && <Button onClick={() => setVerifyAsk(true)}>Verify with tests…</Button>}
        </div>
        {pending === "approve" && <Confirm title="Approve this patch?" confirmLabel="Approve patch" busy={act.isPending} onConfirm={() => act.mutate("approve")} onCancel={() => setPending(null)}>
          <p>Approval records your decision. It does <strong>not</strong> change any file; applying is a separate step.</p></Confirm>}
        {pending === "reject" && <Confirm title="Reject this patch?" confirmLabel="Reject patch" busy={act.isPending} disabled={reason.trim().length < 5} onConfirm={() => act.mutate("reject")} onCancel={() => setPending(null)}>
          <Field label="Reason (at least 5 characters)" htmlFor={`rj-${summary.id}`}><TextInput id={`rj-${summary.id}`} value={reason} onChange={(e) => setReason(e.target.value)} /></Field></Confirm>}
        {pending === "apply" && <Confirm title="Apply this patch to your working tree?" confirmLabel="Apply patch" busy={act.isPending} onConfirm={() => act.mutate("apply")} onCancel={() => setPending(null)}>
          <p>This writes {summary.files.length} file(s): {summary.files.map((f) => f.path).join(", ")}. Nothing is committed. Original content is retained and recovery instructions are provided.</p>
          <p>The patch is applied only if the files still match what was validated.</p></Confirm>}
        {act.error && <MutationError error={act.error} />}
        {["applied", "verification_failed", "verified"].includes(s) && (
          <div className="rounded border border-slate-200 p-2" data-testid="apply-state">
            <p className="font-medium">Applied to your working tree{p?.applied_at ? ` at ${formatDate(p.applied_at)}` : ""}.
              {s === "applied" && " This does not mean it works: run verification."}</p>
            {rollback.data && (
              <details className="mt-1"><summary className="cursor-pointer">Recovery instructions</summary>
                <ul className="list-disc pl-5 text-xs">{rollback.data.instructions.map((i) => <li key={i}>{i}</li>)}</ul>
                {rollback.data.reverse_diff && <pre tabIndex={0} aria-label="Reverse diff" className="mt-1 max-h-60 overflow-auto bg-slate-50 p-2 text-xs">{rollback.data.reverse_diff}</pre>}</details>
            )}
          </div>
        )}
        {verifyAsk && (
          <Confirm title="Run tests to verify this patch?" confirmLabel="Run verification" busy={start.isPending} disabled={!vprev.data} onConfirm={() => start.mutate()} onCancel={() => setVerifyAsk(false)}>
            <p>This runs tests in a restricted local subprocess. It does not run anything else.</p>
            <label className="block text-xs">Scope <select aria-label="Verification scope" value={scope} onChange={(e) => setScope(e.target.value as "selected" | "all")} className="ml-1 rounded border border-slate-300 px-1">
              <option value="selected">Selected tests</option><option value="all">All discovered tests</option></select></label>
            {vprev.isLoading ? <Spinner /> : vprev.data && (
              <ul className="list-disc pl-5 text-xs">{vprev.data.selected.map((t) => <li key={t.target}><span className="font-mono">{t.target}</span> — {t.reason}</li>)}
                {vprev.data.notes.map((n) => <li key={n}>{n}</li>)}</ul>)}
            {vprev.error && <MutationError error={vprev.error} />}
          </Confirm>
        )}
        {start.error && <MutationError error={start.error} />}
        {verifs.data && verifs.data.length > 0 && <div className="space-y-2" aria-label="Verification history">
          <h3 className="font-semibold">Verification history</h3>{verifs.data.map((v) => <VerificationCard key={v.id} v={v} />)}</div>}
      </div>
    </Card>
  );
}

const CRIT_TONE: Record<string, Tone> = { pass: "success", fail: "danger", incomplete: "warning", blocked: "warning", unavailable: "neutral" };

function VerificationCard({ v }: { v: RepairVerification }) {
  const cmp = v.comparison;
  return (
    <div className="rounded border border-slate-200 p-3" data-testid="verification">
      <div className="flex flex-wrap items-center gap-2"><Badge tone={OUTCOME_TONE[v.outcome]}>{v.outcome}</Badge>
        <span className="text-xs text-slate-500">{formatDate(v.created_at)}{v.run?.exit_code !== undefined && v.run?.exit_code !== null ? ` · exit ${v.run.exit_code}` : ""}{v.run?.duration_ms != null ? ` · ${v.run.duration_ms} ms` : ""}</span></div>
      {v.meaning && <p className="mt-1 text-xs text-slate-600">{v.meaning}</p>}
      {v.outcome_reason && <p className="text-sm">{v.outcome_reason}</p>}
      {v.run && <p className="text-xs">{v.run.tests_passed ?? 0} passed · {v.run.tests_failed ?? 0} failed · run {v.run.status}</p>}
      <p className="mt-1 text-xs font-medium">Selected tests</p>
      <ul className="list-disc pl-5 text-xs">{v.selected_tests.map((t) => <li key={t.target}><span className="font-mono">{t.target}</span> — {t.reason}</li>)}</ul>
      <p className="mt-1 text-xs font-medium">Criteria</p>
      <ul className="space-y-0.5 text-xs">{v.criteria.map((c) => <li key={c.id}><Badge tone={CRIT_TONE[c.status]}>{c.status}</Badge> {c.detail}</li>)}</ul>
      <p className="mt-1 text-xs font-medium">Baseline comparison</p>
      {cmp.baseline_available ? (
        <ul className="list-disc pl-5 text-xs">
          <li>New failures: {(cmp.new_failures ?? []).join(", ") || "none"}</li>
          <li>Pre-existing failures: {(cmp.pre_existing_failures ?? []).join(", ") || "none"}</li>
          <li>No longer failing: {(cmp.no_longer_failing ?? []).join(", ") || "none"}</li>
        </ul>
      ) : <p className="text-xs text-slate-500">{cmp.note ?? "No baseline run is linked."}</p>}
      {v.static_check?.available && <p className="text-xs">Static re-check: {v.static_check.new_findings?.length ?? 0} new, {v.static_check.resolved_findings ?? 0} resolved.</p>}
      {v.run && (v.run.stdout_tail || v.run.stderr_tail) && (
        <details className="mt-1"><summary className="cursor-pointer text-xs">Bounded test output{v.run.output_truncated ? " (truncated)" : ""}</summary>
          <pre tabIndex={0} aria-label="Test output" className="max-h-60 overflow-auto bg-slate-50 p-2 text-xs">{v.run.stdout_tail}{v.run.stderr_tail}</pre></details>
      )}
    </div>
  );
}

export function RepairDetailPage() {
  const { repositoryId = "", repairId = "" } = useParams();
  const qc = useQueryClient();
  const [manualDiff, setManualDiff] = useState("");
  const [showManual, setShowManual] = useState(false);
  const d = useQuery({ queryKey: ["repair", repositoryId, repairId], queryFn: () => api.repair(repositoryId, repairId) });
  const patches = useQuery({ queryKey: ["repair-patches", repositoryId, repairId, d.data?.updated_at], queryFn: () => api.repairPatches(repositoryId, repairId), enabled: Boolean(d.data) });
  const audit = useQuery({ queryKey: ["repair-audit", repositoryId, repairId, d.data?.updated_at], queryFn: () => api.repairAudit(repositoryId, repairId), enabled: Boolean(d.data) });
  const done = () => { void qc.invalidateQueries({ queryKey: ["repair"] }); void qc.invalidateQueries({ queryKey: ["repair-patches"] }); void qc.invalidateQueries({ queryKey: ["repairs"] }); };
  const ws = useQuery({ queryKey: ["workspace"], queryFn: api.workspace });
  const aiEnabled = ws.data?.ai_provider === "anthropic";
  const create = useMutation({ mutationFn: (source: "generated" | "manual" | "ai_assisted") => api.createPatch(repositoryId, repairId, source === "manual" ? { source, diff: manualDiff } : { source }), onSuccess: () => { setManualDiff(""); setShowManual(false); done(); } });
  const close = useMutation({ mutationFn: (s: "open" | "closed") => api.setRepairStatus(repositoryId, repairId, s), onSuccess: done });
  if (d.isLoading) return <Spinner label="Loading investigation…" />;
  if (d.isError || !d.data) return <ErrorState error={d.error} onRetry={() => void d.refetch()} />;
  const r = d.data;
  const canPatch = !["closed", "applied", "verified"].includes(r.status);
  return (
    <>
      <PageHeader
        title={r.title}
        subtitle={r.description || undefined}
        actions={<><ButtonLink to={`/repositories/${repositoryId}/repairs`} variant="secondary">All investigations</ButtonLink>
          <Button variant="secondary" onClick={() => close.mutate(r.status === "closed" ? "open" : "closed")} disabled={close.isPending}>{r.status === "closed" ? "Reopen" : "Close"}</Button></>}
      />
      <div className="flex flex-wrap items-center gap-2" data-testid="repair-status">
        <RepairStatusBadge status={r.status} /><Badge tone={SEV_TONE[r.severity] ?? "neutral"}>{r.severity}</Badge>
        {r.verification_outcome && <Badge tone={OUTCOME_TONE[r.verification_outcome]}>verification {r.verification_outcome}</Badge>}
        {r.status === "applied" && <span className="text-sm text-amber-700">Applied but not yet verified.</span>}
      </div>
      {close.error && <MutationError error={close.error} />}
      <Evidence repositoryId={repositoryId} id={repairId} />
      <Card title="Patches" actions={canPatch ? <div className="flex gap-2">
        <Button variant="secondary" onClick={() => create.mutate("generated")} disabled={create.isPending || !r.proposal.patch_available}>Create suggested patch</Button>
        {aiEnabled && <Button variant="secondary" onClick={() => create.mutate("ai_assisted")} disabled={create.isPending}>Propose a fix with Claude</Button>}
        <Button variant="secondary" onClick={() => setShowManual((v) => !v)}>Supply my own patch</Button></div> : undefined}>
        {create.isPending && create.variables === "ai_assisted" && <p role="status" className="text-sm text-slate-600">Asking Claude for a proposal. This can take a minute…</p>}
        {create.error && <MutationError error={create.error} />}
        {aiEnabled && canPatch && <p className="text-xs text-slate-500">Claude sees this investigation, its evidence and its relevant files (secrets scrubbed). Its proposal is only a draft: NOVA checks every edit, then it must be validated, approved and verified like any other patch.</p>}
        {!r.proposal.patch_available && canPatch && <p className="text-xs text-slate-500">A suggested patch is unavailable until the proposal reports a verified deterministic fix.</p>}
        {showManual && (
          <div className="mb-3 space-y-2">
            <Field label="Unified diff" htmlFor="manual-diff" hint="Text files only; paths must be inside the repository. It is validated before it can be approved."><TextArea id="manual-diff" rows={8} className="font-mono text-xs" value={manualDiff} onChange={(e) => setManualDiff(e.target.value)} /></Field>
            <Button onClick={() => create.mutate("manual")} disabled={create.isPending || !manualDiff.trim()}>Save patch draft</Button>
          </div>
        )}
        {patches.data && patches.data.length === 0 && <EmptyState title="No patches">Patches you create appear here for review.</EmptyState>}
      </Card>
      {patches.data?.map((p) => <PatchPanel key={p.id} repositoryId={repositoryId} repairId={repairId} summary={p} />)}
      <Card title="Audit history">
        {audit.data && audit.data.items.length > 0 ? (
          <ul className="divide-y divide-slate-100 text-sm">{audit.data.items.map((a) => <li key={a.id} className="py-1"><span className="font-mono text-xs">{a.action}</span> <span className="text-xs text-slate-500">{formatDate(a.created_at)}</span></li>)}</ul>
        ) : <p className="text-sm text-slate-500">No audit events.</p>}
      </Card>
    </>
  );
}
