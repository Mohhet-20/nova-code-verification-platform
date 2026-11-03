import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { RequestGovernanceReview } from "../components/RequestGovernanceReview";
import { Badge, Button, ButtonLink, Card, EmptyState, ErrorState, Field, Notice, PageHeader, Pagination, Spinner, Stat, TextArea, TextInput } from "../components/ui";
import { ApiError, api, describeError } from "../services/api";
import { formatDate } from "../services/format";
import type { ReviewFinding, ReviewFindingStatus, ReviewMode, ReviewStatus } from "../types/api";

type Tone = "success" | "danger" | "warning" | "info" | "neutral";
const STATUS_TONE: Record<ReviewStatus, Tone> = { pending: "info", running: "info", completed: "success", completed_with_warnings: "warning", failed: "danger" };
const STATUS_LABEL: Record<ReviewStatus, string> = {
  pending: "Pending", running: "Running", completed: "Completed", completed_with_warnings: "Completed with warnings", failed: "Failed",
};
const SEV_TONE: Record<string, Tone> = { critical: "danger", high: "danger", medium: "warning", low: "info", info: "neutral" };
const RISK_TONE: Record<string, Tone> = { critical: "danger", high: "danger", moderate: "warning", low: "success" };
const MODES: { value: ReviewMode; label: string; help: string }[] = [
  { value: "working_tree", label: "Working tree vs HEAD", help: "Everything changed since the last commit, staged or not." },
  { value: "staged", label: "Staged changes", help: "Only what is in the Git index, compared with HEAD." },
  { value: "unstaged", label: "Unstaged changes", help: "Edits in the working tree that are not staged yet." },
  { value: "revisions", label: "Compare two revisions", help: "A base revision against a head revision (default HEAD)." },
];
const CATEGORIES = ["", "correctness", "security", "reliability", "maintainability", "performance", "testing_gap", "informational"];
const SEVERITIES = ["", "critical", "high", "medium", "low", "info"];
const STATUSES = ["", "open", "accepted", "dismissed", "follow_up"];

function MutationError({ error }: { error: unknown }) {
  if (error instanceof ApiError && error.status === 403)
    return <Notice tone="warning">Your role does not permit this action. You can still read results.</Notice>;
  return <Notice tone="danger">{describeError(error)}</Notice>;
}

export function ReviewStatusBadge({ status }: { status: ReviewStatus }) {
  return <Badge tone={STATUS_TONE[status]}>{STATUS_LABEL[status]}</Badge>;
}

// ------------------------------------------------------------------------------------------------ list + creation

export function ReviewsPage() {
  const { repositoryId = "" } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [page, setPage] = useState(1);
  const [mode, setMode] = useState<ReviewMode>("working_tree");
  const [base, setBase] = useState("");
  const [head, setHead] = useState("");
  const [paths, setPaths] = useState("");
  const body = {
    mode,
    ...(mode === "revisions" ? { base: base.trim(), head: head.trim() || undefined } : {}),
    paths: paths.split("\n").map((p) => p.trim()).filter(Boolean),
  };
  const repo = useQuery({ queryKey: ["repository", repositoryId], queryFn: () => api.getRepository(repositoryId) });
  const list = useQuery({ queryKey: ["reviews", repositoryId, page], queryFn: () => api.reviews(repositoryId, page) });
  const preview = useMutation({ mutationFn: () => api.reviewPreview(repositoryId, body) });
  const create = useMutation({
    mutationFn: () => api.createReview(repositoryId, body),
    onSuccess: (r) => { void qc.invalidateQueries({ queryKey: ["reviews"] }); navigate(`/repositories/${repositoryId}/reviews/${r.id}`); },
  });
  const help = MODES.find((m) => m.value === mode)?.help;
  return (
    <>
      <PageHeader
        title="Code review"
        subtitle={repo.data ? `Review local Git changes in ${repo.data.name}: changed symbols, findings, change risk, impact and recommended tests. Nothing is executed.` : undefined}
        actions={<ButtonLink to={`/repositories/${repositoryId}`} variant="secondary">Back to repository</ButtonLink>}
      />
      <Card title="Start a review">
        <div className="space-y-3">
          <Notice tone="info">
            A review reads your local Git repository (diff, file contents and the code index) and applies static checks and documented rules. It does not run tests or your code,
            does not change the working tree or Git history, and does not contact any hosted pull-request service. No AI provider is used.
          </Notice>
          <Field label="What to review" htmlFor="rv-mode" hint={help}>
            <select id="rv-mode" value={mode} onChange={(e) => { setMode(e.target.value as ReviewMode); preview.reset(); }} className="rounded border border-slate-300 px-2 py-1.5 text-sm">
              {MODES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
            </select>
          </Field>
          {mode === "revisions" && (
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Base revision" htmlFor="rv-base" hint="Branch, tag, or commit, e.g. main or HEAD~1">
                <TextInput id="rv-base" value={base} onChange={(e) => setBase(e.target.value)} />
              </Field>
              <Field label="Head revision (optional)" htmlFor="rv-head" hint="Defaults to HEAD">
                <TextInput id="rv-head" value={head} onChange={(e) => setHead(e.target.value)} />
              </Field>
            </div>
          )}
          <Field label="Only these files (optional)" htmlFor="rv-paths" hint="One repository-relative path per line; leave empty for all changed files.">
            <TextArea id="rv-paths" rows={2} value={paths} onChange={(e) => setPaths(e.target.value)} />
          </Field>
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" onClick={() => preview.mutate()} disabled={preview.isPending || (mode === "revisions" && !base.trim())}>
              {preview.isPending ? "Checking…" : "Preview what will be analysed"}
            </Button>
            <Button onClick={() => create.mutate()} disabled={create.isPending || (mode === "revisions" && !base.trim())}>
              {create.isPending ? "Starting…" : "Start review"}
            </Button>
          </div>
          {preview.error && <MutationError error={preview.error} />}
          {create.error && <MutationError error={create.error} />}
          {preview.data && (
            <div className="rounded border border-slate-200 bg-slate-50 p-3 text-sm" data-testid="review-preview">
              <p>{preview.data.explanation}</p>
              <p className="mt-1 font-medium">
                {preview.data.total_files === 0 ? "No changed files were found for this selection." : `${preview.data.total_files} changed file(s) will be analysed.`}
                {preview.data.omitted > 0 && ` ${preview.data.omitted} beyond the limit of ${preview.data.limits.max_files} will not be.`}
              </p>
              <ul className="mt-1 max-h-40 overflow-auto font-mono text-xs">
                {preview.data.files.slice(0, 50).map((f) => <li key={f.rel_path}>{f.change_type} · {f.rel_path}</li>)}
              </ul>
              {preview.data.warnings.map((w) => <p key={w} className="mt-1 text-amber-700">{w}</p>)}
            </div>
          )}
        </div>
      </Card>
      <Card title="Previous reviews">
        {list.isLoading ? <Spinner label="Loading reviews…" /> : list.isError ? <ErrorState error={list.error} onRetry={() => void list.refetch()} /> : list.data!.items.length === 0 ? (
          <EmptyState title="No reviews yet">Start a review above to analyse your local changes.</EmptyState>
        ) : (
          <>
            <ul className="divide-y divide-slate-100">
              {list.data!.items.map((r) => (
                <li key={r.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                  <div>
                    <Link className="font-medium text-indigo-700 hover:underline" to={`/repositories/${repositoryId}/reviews/${r.id}`}>
                      {r.mode.replace("_", " ")}{r.base_ref ? ` · ${r.base_ref}…${r.head_ref ?? "HEAD"}` : ""}
                    </Link>
                    <div className="text-xs text-slate-500">{formatDate(r.created_at)} · {r.files_total} file(s) · {r.findings_total} finding(s)</div>
                  </div>
                  <div className="flex items-center gap-2">
                    {r.risk_category && <Badge tone={RISK_TONE[r.risk_category] ?? "neutral"}>{r.risk_category} change risk</Badge>}
                    <ReviewStatusBadge status={r.status} />
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

type Tab = "summary" | "changes" | "findings" | "risk" | "impact" | "tests";
const TABS: { id: Tab; label: string }[] = [
  { id: "summary", label: "Summary" }, { id: "changes", label: "Changes" }, { id: "findings", label: "Findings" },
  { id: "risk", label: "Risk" }, { id: "impact", label: "Impact" }, { id: "tests", label: "Test recommendations" },
];

export function ReviewDetailPage() {
  const { repositoryId = "", reviewId = "" } = useParams();
  const [tab, setTab] = useState<Tab>("summary");
  const summary = useQuery({
    queryKey: ["review-summary", repositoryId, reviewId],
    queryFn: () => api.reviewSummary(repositoryId, reviewId),
    refetchInterval: (q) => (q.state.data && !q.state.data.ready ? 1500 : false),
  });
  const back = <ButtonLink to={`/repositories/${repositoryId}/reviews`} variant="secondary">All reviews</ButtonLink>;
  if (summary.isLoading) return <><PageHeader title="Code review" actions={back} /><Spinner label="Loading review…" /></>;
  if (summary.isError) return <><PageHeader title="Code review" actions={back} /><ErrorState error={summary.error} onRetry={() => void summary.refetch()} /></>;
  const { review: r, summary: s, ready, stages } = summary.data!;
  const failedStages = stages.filter((x) => x.status === "failed");
  return (
    <>
      <PageHeader
        title="Code review"
        subtitle={`${r.mode.replace("_", " ")}${r.base_ref ? ` · ${r.base_ref}…${r.head_ref ?? "HEAD"}` : ""} · ${formatDate(r.created_at)}`}
        actions={<>{back}<RequestGovernanceReview resourceType="code_review" resourceId={r.id} /></>}
      />
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <ReviewStatusBadge status={r.status} />
        {r.index_stale && <Badge tone="warning">Index stale or missing</Badge>}
        {r.files_omitted > 0 && <Badge tone="warning">{r.files_omitted} file(s) not analysed</Badge>}
      </div>
      {!ready && <Notice tone="info">The review is {r.status}. This page updates automatically.</Notice>}
      {r.status === "failed" && <Notice tone="danger">{r.error_message ?? "The review failed."}</Notice>}
      {r.status === "completed_with_warnings" && (
        <Notice tone="warning">
          <p className="font-medium">Partial results: some analysis did not complete fully.</p>
          <ul className="list-disc pl-5">{r.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
          {failedStages.length > 0 && <p className="mt-1">Failed stage(s): {failedStages.map((x) => x.name).join(", ")}.</p>}
        </Notice>
      )}
      <div role="tablist" className="my-3 flex flex-wrap gap-1 border-b border-slate-200">
        {TABS.map((t) => (
          <button key={t.id} role="tab" aria-selected={tab === t.id} onClick={() => setTab(t.id)}
            className={`px-3 py-1.5 text-sm ${tab === t.id ? "border-b-2 border-indigo-600 font-medium text-indigo-700" : "text-slate-600 hover:text-slate-900"}`}>
            {t.label}
          </button>
        ))}
      </div>
      {tab === "summary" && (
        <Card title="Summary">
          <p className="mb-3 text-sm">{s.headline ?? "No summary is available yet."}</p>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Files changed" value={r.files_total} hint={`${r.files_analyzed} analysed`} />
            <Stat label="Lines" value={`+${r.lines_added} / −${r.lines_removed}`} />
            <Stat label="Changed symbols" value={r.symbols_changed} />
            <Stat label="Findings" value={r.findings_total} />
          </div>
          {s.by_severity && Object.keys(s.by_severity).length > 0 && (
            <p className="mt-3 text-sm">By severity: {Object.entries(s.by_severity).map(([k, v]) => `${k} ${v}`).join(", ")}</p>
          )}
          {s.clean_review_note && <Notice tone="info">{s.clean_review_note}</Notice>}
          {s.ai && <p className="mt-2 text-xs text-slate-500">{s.ai.note}</p>}
          <h3 className="mt-4 text-sm font-semibold">Pipeline stages</h3>
          <ul className="text-sm">
            {stages.map((st) => (
              <li key={st.name}>
                <Badge tone={st.status === "ok" ? "success" : st.status === "failed" ? "danger" : "warning"}>{st.status}</Badge> {st.name}
                {st.detail ? <span className="text-slate-500"> — {st.detail}</span> : null}
              </li>
            ))}
          </ul>
        </Card>
      )}
      {tab === "changes" && <ChangesTab repositoryId={repositoryId} reviewId={reviewId} ready={ready} />}
      {tab === "findings" && <FindingsTab repositoryId={repositoryId} reviewId={reviewId} ready={ready} />}
      {tab === "risk" && <RiskTab repositoryId={repositoryId} reviewId={reviewId} />}
      {tab === "impact" && <ImpactTab repositoryId={repositoryId} reviewId={reviewId} />}
      {tab === "tests" && <TestsTab repositoryId={repositoryId} reviewId={reviewId} />}
    </>
  );
}

function Pending() {
  return <Notice tone="info">Results will appear when the review finishes.</Notice>;
}

function ChangesTab({ repositoryId, reviewId, ready }: { repositoryId: string; reviewId: string; ready: boolean }) {
  const [page, setPage] = useState(1);
  const q = useQuery({ queryKey: ["review-changes", repositoryId, reviewId, page, ready], queryFn: () => api.reviewChanges(repositoryId, reviewId, page) });
  if (q.isLoading) return <Spinner label="Loading changes…" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  if (q.data!.items.length === 0) return <EmptyState title="No changed files">{ready ? "Nothing differs for this selection." : "Waiting for the review to finish."}</EmptyState>;
  return (
    <Card title="Changed files">
      <ul className="divide-y divide-slate-100 text-sm">
        {q.data!.items.map((c) => (
          <li key={c.id} className="py-2">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={c.change_type === "deleted" ? "danger" : c.change_type === "added" ? "success" : "info"}>{c.change_type}</Badge>
              <span className="font-mono">{c.rel_path}</span>
              {c.old_path && c.old_path !== c.rel_path && <span className="text-xs text-slate-500">from {c.old_path}</span>}
              {c.is_test && <Badge tone="neutral">test</Badge>}
              {c.lines_added !== null && <span className="text-xs text-slate-500">+{c.lines_added} / −{c.lines_removed}</span>}
            </div>
            {c.extraction_status !== "ok" && <p className="text-xs text-amber-700">Not analysed in detail: {c.extraction_status.replace("_", " ")}.</p>}
            {c.notes.map((n) => <p key={n} className="text-xs text-slate-500">{n}</p>)}
            {c.symbols.length > 0 && (
              <ul className="mt-1 pl-4 text-xs">
                {c.symbols.map((sy) => (
                  <li key={sy.qualified_name + sy.change_kind}>
                    <span className="font-mono">{sy.qualified_name}</span> · {sy.change_kind}{sy.is_public ? " · public" : ""}
                    {sy.mapping === "fresh_parse" && <span className="text-amber-700"> · parsed from the diff; not linked to the (stale) index</span>}
                    {sy.signature_old && sy.signature_new && sy.signature_old !== sy.signature_new && <span className="text-slate-500"> · {sy.signature_old} → {sy.signature_new}</span>}
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
      <Pagination page={q.data!.page} pages={q.data!.pages} total={q.data!.total} onPage={setPage} />
    </Card>
  );
}

function FindingCard({ f, onAct, busy, error }: { f: ReviewFinding; onAct: (status: ReviewFindingStatus, rationale?: string) => void; busy: boolean; error: unknown }) {
  const [rationale, setRationale] = useState("");
  return (
    <li className="py-3 text-sm" data-testid="review-finding">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={SEV_TONE[f.severity] ?? "neutral"}>{f.severity}</Badge>
        <Badge tone="neutral">{f.category.replace("_", " ")}</Badge>
        <Badge tone="neutral">{f.confidence} confidence</Badge>
        <Badge tone="neutral">{f.source === "analyzer" ? "static analysis" : "rule-based check"}</Badge>
        <Badge tone={f.status === "open" ? "info" : f.status === "dismissed" ? "neutral" : "success"}>{f.status.replace("_", " ")}</Badge>
      </div>
      <p className="mt-1 font-medium">{f.title}</p>
      <p className="font-mono text-xs text-slate-500">{f.rel_path}{f.start_line ? `:${f.start_line}` : ""} · {f.rule_id}</p>
      <p className="mt-1">{f.explanation}</p>
      {f.evidence && <p className="mt-1 text-xs"><span className="font-semibold">Evidence: </span>{f.evidence}</p>}
      {f.remediation && <p className="mt-1 text-xs"><span className="font-semibold">Suggested action: </span>{f.remediation}</p>}
      {f.limitations && <p className="mt-1 text-xs text-slate-500"><span className="font-semibold">Limits: </span>{f.limitations}</p>}
      {f.rationale && <p className="mt-1 text-xs italic">Rationale: {f.rationale}{f.inherited_from_review_id ? " (carried over from an earlier review)" : ""}</p>}
      <div className="mt-2 space-y-2">
        <TextInput aria-label={`Rationale for ${f.title}`} placeholder="Rationale (required to dismiss)" value={rationale} onChange={(e) => setRationale(e.target.value)} />
        <div className="flex flex-wrap gap-2">
          <Button variant="secondary" disabled={busy} onClick={() => onAct("accepted", rationale || undefined)}>Accept</Button>
          <Button variant="secondary" disabled={busy} onClick={() => onAct("dismissed", rationale)}>Dismiss</Button>
          <Button variant="secondary" disabled={busy} onClick={() => onAct("follow_up", rationale || undefined)}>Follow up</Button>
          {f.status !== "open" && <Button variant="secondary" disabled={busy} onClick={() => onAct("open")}>Reopen</Button>}
        </div>
        {error != null && <MutationError error={error} />}
      </div>
    </li>
  );
}

function FindingsTab({ repositoryId, reviewId, ready }: { repositoryId: string; reviewId: string; ready: boolean }) {
  const qc = useQueryClient();
  const [page, setPage] = useState(1);
  const [severity, setSeverity] = useState("");
  const [category, setCategory] = useState("");
  const [status, setStatus] = useState("");
  const q = useQuery({
    queryKey: ["review-findings", repositoryId, reviewId, page, severity, category, status, ready],
    queryFn: () => api.reviewFindings(repositoryId, reviewId, { page, severity, category, status }),
  });
  const act = useMutation({
    mutationFn: (v: { id: string; status: ReviewFindingStatus; rationale?: string }) =>
      api.setReviewFinding(repositoryId, reviewId, v.id, { status: v.status, rationale: v.rationale }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["review-findings"] }); },
  });
  const filtered = Boolean(severity || category || status);
  const sel = (label: string, value: string, set: (v: string) => void, opts: string[]) => (
    <label className="text-xs">{label}
      <select aria-label={label} value={value} onChange={(e) => { set(e.target.value); setPage(1); }} className="ml-1 rounded border border-slate-300 px-1 py-1 text-sm">
        {opts.map((o) => <option key={o} value={o}>{o ? o.replace("_", " ") : "all"}</option>)}
      </select>
    </label>
  );
  return (
    <Card title="Findings">
      <div className="mb-3 flex flex-wrap gap-3">
        {sel("Severity", severity, setSeverity, SEVERITIES)}
        {sel("Category", category, setCategory, CATEGORIES)}
        {sel("Status", status, setStatus, STATUSES)}
      </div>
      {q.isLoading ? <Spinner label="Loading findings…" /> : q.isError ? <ErrorState error={q.error} onRetry={() => void q.refetch()} /> : q.data!.items.length === 0 ? (
        <EmptyState title={filtered ? "No findings match these filters" : "No findings"}>
          {filtered ? "Clear a filter to see more." : ready ? "The enabled checks produced no findings. This does not show that the change is free of defects." : "Waiting for the review to finish."}
        </EmptyState>
      ) : (
        <>
          <ul className="divide-y divide-slate-100">
            {q.data!.items.map((f) => (
              <FindingCard key={f.id} f={f} busy={act.isPending} error={act.variables?.id === f.id ? act.error : null}
                onAct={(st, rationale) => act.mutate({ id: f.id, status: st, rationale })} />
            ))}
          </ul>
          <Pagination page={q.data!.page} pages={q.data!.pages} total={q.data!.total} onPage={setPage} />
        </>
      )}
    </Card>
  );
}

function RiskTab({ repositoryId, reviewId }: { repositoryId: string; reviewId: string }) {
  const q = useQuery({ queryKey: ["review-risk", repositoryId, reviewId], queryFn: () => api.reviewRisk(repositoryId, reviewId) });
  if (q.isLoading) return <Spinner label="Loading risk…" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  const r = q.data!;
  if (!r.ready) return <Pending />;
  return (
    <Card title="Change risk">
      {r.state === "ranked" && r.score !== null ? (
        <p className="text-sm">
          <Badge tone={RISK_TONE[r.category ?? ""] ?? "neutral"}>{r.category}</Badge>{" "}
          <span className="font-semibold">Score {r.score.toFixed(1)} / 100</span>
        </p>
      ) : (
        <Notice tone="warning">Risk could not be ranked: {r.reason ?? "there was not enough evidence."}</Notice>
      )}
      <p className="mt-2 text-xs text-slate-600">{r.disclaimer}</p>
      <p className="mt-1 text-xs text-slate-600">Method: <code>{r.formula}</code></p>
      <table className="mt-3 w-full text-left text-xs">
        <thead><tr><th className="py-1">Factor</th><th>Status</th><th>Weight</th><th>Contribution</th><th>Evidence</th></tr></thead>
        <tbody>
          {r.factors.map((f) => (
            <tr key={f.id} className="border-t border-slate-100 align-top">
              <td className="py-1 pr-2 font-medium">{f.label}</td>
              <td>{f.status === "measured" ? "Measured" : "Unavailable"}</td>
              <td>{f.weight}</td>
              <td>{f.contribution === null ? "—" : f.contribution.toFixed(2)}</td>
              <td>{f.detail}{f.evidence.length > 0 && <span className="text-slate-500"> ({f.evidence.slice(0, 3).join("; ")})</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {r.notes?.map((n) => <p key={n} className="mt-1 text-xs text-slate-500">{n}</p>)}
    </Card>
  );
}

function ImpactTab({ repositoryId, reviewId }: { repositoryId: string; reviewId: string }) {
  const q = useQuery({ queryKey: ["review-impact", repositoryId, reviewId], queryFn: () => api.reviewImpact(repositoryId, reviewId) });
  if (q.isLoading) return <Spinner label="Loading impact…" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  const i = q.data!;
  if (!i.ready) return <Pending />;
  return (
    <Card title="Impact">
      {(i.index_stale || !i.complete) && (
        <Notice tone="warning">
          This impact picture may be incomplete.
          <ul className="list-disc pl-5">{[...i.index_notes, ...i.limitations].map((n) => <li key={n}>{n}</li>)}</ul>
        </Notice>
      )}
      <h3 className="mt-2 text-sm font-semibold">Dependents (to depth {i.max_depth})</h3>
      {i.dependents.length === 0 ? <p className="text-sm text-slate-600">No dependents were found in the index.</p> : (
        <ul className="text-sm">{i.dependents.map((d) => <li key={d.rel_path}><span className="font-mono">{d.rel_path}</span> · depth {d.depth} · {d.relationship.replace("_", " ")}</li>)}</ul>
      )}
      <h3 className="mt-3 text-sm font-semibold">Potentially affected tests</h3>
      {i.affected_tests.length === 0 ? <p className="text-sm text-slate-600">No tests were linked to the changed files.</p> : (
        <ul className="text-sm">{i.affected_tests.map((t) => <li key={t.rel_path}><span className="font-mono">{t.rel_path}</span> <span className="text-xs text-slate-500">— {t.note}</span></li>)}</ul>
      )}
    </Card>
  );
}

function TestsTab({ repositoryId, reviewId }: { repositoryId: string; reviewId: string }) {
  const q = useQuery({ queryKey: ["review-recs", repositoryId, reviewId], queryFn: () => api.reviewRecommendations(repositoryId, reviewId) });
  if (q.isLoading) return <Spinner label="Loading recommendations…" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  const d = q.data!;
  if (!d.ready) return <Pending />;
  return (
    <Card title="Test recommendations">
      <Notice tone="info">{d.note}</Notice>
      {d.items.length === 0 ? <EmptyState title="No recommendations">No test recommendations follow from this change.</EmptyState> : (
        <ul className="divide-y divide-slate-100 text-sm">
          {d.items.map((r, idx) => (
            <li key={idx} className="py-2">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={r.priority === "high" ? "danger" : r.priority === "medium" ? "warning" : "neutral"}>{r.priority} priority</Badge>
                <Badge tone="neutral">{r.confidence} confidence</Badge>
                <span className="font-mono text-xs">{r.target.rel_path}{r.target.symbol ? ` · ${r.target.symbol}` : ""}</span>
              </div>
              <p className="mt-1">{r.behavior}</p>
              <p className="text-xs text-slate-600">Why: {r.reason}</p>
              <p className="text-xs text-slate-600">Existing tests: {r.existing_tests.status.replace(/_/g, " ")}{r.existing_tests.note ? ` — ${r.existing_tests.note}` : ""}</p>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
