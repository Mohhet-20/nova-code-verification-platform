import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Badge, Button, ButtonLink, Card, EmptyState, ErrorState, Field, Notice, PageHeader, Pagination, Spinner, Stat, TextArea, TextInput } from "../components/ui";
import { api } from "../services/api";
import { formatDate } from "../services/format";
import type { CvBaseline, CvMode, CvPolicy, CvPreview, CvSource } from "../types/api";
import { MutationError } from "./RepairsPage";
import { CHECK_TONE, GateBadge, shortSha } from "./VerificationShared";

const SOURCES: Record<CvSource, string> = {
  working_tree: "Working tree (uncommitted changes)", staged: "Staged changes", revisions: "Between two revisions", patch: "A unified diff (inspected, never applied)", manifest: "A manual list of paths",
};

function PlanView({ preview }: { preview: CvPreview }) {
  const { change_set: cs, plan } = preview;
  return (
    <div className="mt-3 space-y-2 text-sm" data-testid="plan-preview">
      <p><span className="font-semibold">{cs.counts.total} changed file(s)</span> ({cs.counts.python} Python, {cs.counts.docs} docs) · mode <strong>{plan.mode}</strong>: {plan.mode_meaning}</p>
      {cs.warnings.map((w) => <Notice key={w} tone="warning">{w}</Notice>)}
      {plan.binding_problems.map((w) => <Notice key={w} tone="warning">{w}</Notice>)}
      <ul aria-label="Changed files" className="max-h-40 overflow-auto rounded border border-slate-200 divide-y divide-slate-100">
        {cs.files.length === 0 ? <li className="p-2 text-slate-600">No changes detected — there is nothing to verify; a run would be BLOCKED.</li> : cs.files.map((f) => <li key={f.path} className="flex gap-2 px-2 py-1"><Badge>{f.status}</Badge><code>{f.path}</code></li>)}
      </ul>
      <ul aria-label="Planned checks" className="space-y-1">
        {plan.checks.map((c) => (
          <li key={c.check_id} className="flex flex-wrap items-center gap-2">
            <Badge tone={CHECK_TONE[c.status] ?? "neutral"}>{c.status}</Badge><code>{c.check_id}</code><span className="text-xs text-slate-500">tier {c.tier} · {c.required ? "required" : "optional"} · confidence {c.confidence}</span>
            <span className="text-slate-700">{c.reason}</span>
          </li>
        ))}
      </ul>
      {plan.unverified_paths.length > 0 && <p className="text-slate-600">No verifier exists for: {plan.unverified_paths.slice(0, 5).join(", ")}</p>}
    </div>
  );
}

function PolicyCard({ repositoryId, policy }: { repositoryId: string; policy: CvPolicy }) {
  const qc = useQueryClient();
  const r = policy.rules;
  const [mode, setMode] = useState<CvMode>(r.default_mode);
  const [minTests, setMinTests] = useState(String(r.tests.min_tests_executed));
  const [maxNew, setMaxNew] = useState(String(r.static.max_new_findings));
  const [pre, setPre] = useState(Boolean(r.tests.allow_preexisting_failures));
  const [baselineReq, setBaselineReq] = useState(Boolean(r.baseline.required));
  const [note, setNote] = useState("");
  const save = useMutation({
    mutationFn: () => api.cvPutPolicy(repositoryId, { ...r, default_mode: mode, static: { ...r.static, max_new_findings: Number(maxNew) }, tests: { ...r.tests, min_tests_executed: Number(minTests), allow_preexisting_failures: pre }, baseline: { required: baselineReq } }, note),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["cv-policy", repositoryId] }); void qc.invalidateQueries({ queryKey: ["cv-overview", repositoryId] }); setNote(""); },
  });
  const ro = !policy.can_edit;
  return (
    <Card title={`Quality-gate policy (version ${policy.version}${policy.is_default ? ", built-in default" : ""})`}>
      <p className="mb-2 text-sm text-slate-600">Precedence when criteria disagree: {policy.precedence.join(" > ")}. Each run keeps a snapshot of the policy it started with. {ro && "Only a workspace owner can change the policy."}</p>
      <form className="grid gap-3 sm:grid-cols-2" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
        <Field label="Default mode" htmlFor="p-mode"><select id="p-mode" disabled={ro} value={mode} onChange={(e) => setMode(e.target.value as CvMode)} className="w-full rounded border border-slate-300 p-1.5 text-sm">{["focused", "standard", "full"].map((m) => <option key={m}>{m}</option>)}</select></Field>
        <Field label="Minimum tests executed" htmlFor="p-min"><TextInput id="p-min" type="number" min={1} disabled={ro} value={minTests} onChange={(e) => setMinTests(e.target.value)} /></Field>
        <Field label="Max new static findings" htmlFor="p-new"><TextInput id="p-new" type="number" min={0} disabled={ro} value={maxNew} onChange={(e) => setMaxNew(e.target.value)} /></Field>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" disabled={ro} checked={pre} onChange={(e) => setPre(e.target.checked)} />Accept pre-existing failures</label>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" disabled={ro} checked={baselineReq} onChange={(e) => setBaselineReq(e.target.checked)} />Require a baseline</label>
        {!ro && (
          <div className="sm:col-span-2 flex flex-wrap items-end gap-2">
            <Field label="Change note" htmlFor="p-note"><TextInput id="p-note" value={note} onChange={(e) => setNote(e.target.value)} maxLength={300} /></Field>
            <Button type="submit" disabled={save.isPending}>{save.isPending ? "Saving…" : "Save new version"}</Button>
          </div>
        )}
      </form>
      <MutationError error={save.error} />
    </Card>
  );
}

function BaselineRow({ b }: { b: CvBaseline }) {
  return (
    <li className="flex flex-wrap items-center gap-2 py-2 text-sm" data-testid={`baseline-${b.id}`}>
      <span className="font-medium">{b.label || "(unlabelled)"}</span>
      <Badge tone={b.status === "valid" && b.integrity_ok ? "success" : "danger"}>{b.integrity_ok ? b.status : "integrity check failed"}</Badge>
      <code className="text-xs">{shortSha(b.head_sha)}</code>
      <span className="text-xs text-slate-500">{b.totals.passed ?? 0} passed · {b.totals.failed ?? 0} failed · {b.totals.collected ?? 0} collected · {formatDate(b.created_at)}</span>
      {b.failing.length > 0 && <span className="text-xs text-amber-700">{b.failing.length} failing test(s) recorded</span>}
    </li>
  );
}

export function VerificationPage() {
  const { repositoryId = "" } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [source, setSource] = useState<CvSource>("working_tree");
  const [mode, setMode] = useState<"" | CvMode>("");
  const [base, setBase] = useState("");
  const [head, setHead] = useState("");
  const [paths, setPaths] = useState("");
  const [patch, setPatch] = useState("");
  const [label, setLabel] = useState("");
  const [page, setPage] = useState(1);
  const [outcome, setOutcome] = useState("");
  const [preview, setPreview] = useState<CvPreview | null>(null);
  const overview = useQuery({ queryKey: ["cv-overview", repositoryId], queryFn: () => api.cvOverview(repositoryId), refetchInterval: (q) => (q.state.data?.active_run ? 2000 : false) });
  const policy = useQuery({ queryKey: ["cv-policy", repositoryId], queryFn: () => api.cvPolicy(repositoryId) });
  const baselines = useQuery({ queryKey: ["cv-baselines", repositoryId], queryFn: () => api.cvBaselines(repositoryId) });
  const runs = useQuery({ queryKey: ["cv-runs", repositoryId, page, outcome], queryFn: () => api.cvRuns(repositoryId, { page, outcome: outcome || undefined, kind: "verify" }) });
  const body = () => ({
    source, mode: mode || undefined, base: source === "revisions" ? base.trim() || undefined : undefined, head: source === "revisions" ? head.trim() || undefined : undefined,
    paths: source === "manifest" ? paths.split("\n").map((p) => p.trim()).filter(Boolean) : undefined, patch: source === "patch" ? patch : undefined,
  });
  const doPreview = useMutation({ mutationFn: () => api.cvPreview(repositoryId, body()), onSuccess: setPreview });
  const start = useMutation({
    mutationFn: () => api.cvStart(repositoryId, { ...body(), confirm: true }),
    onSuccess: (r) => { void qc.invalidateQueries({ queryKey: ["cv-runs"] }); void qc.invalidateQueries({ queryKey: ["cv-overview"] }); navigate(`/repositories/${repositoryId}/verification/${r.id}`); },
  });
  const rec = useMutation({
    mutationFn: () => api.cvCreateBaseline(repositoryId, label.trim(), false),
    onSuccess: () => { setLabel(""); void qc.invalidateQueries({ queryKey: ["cv-baselines"] }); void qc.invalidateQueries({ queryKey: ["cv-overview"] }); },
  });
  const ov = overview.data;
  return (
    <>
      <PageHeader title="Continuous Verification Center" subtitle="Detect changes, plan checks, run them under the Phase 05 runner, compare with a baseline and read the quality-gate decision."
        actions={<ButtonLink to={`/repositories/${repositoryId}`} variant="secondary">Back to repository</ButtonLink>} />
      {overview.isLoading ? <Spinner label="Loading overview…" /> : overview.isError ? <ErrorState error={overview.error} onRetry={() => void overview.refetch()} /> : ov && (
        <Card title="Overview">
          <div className="grid gap-3 sm:grid-cols-5">
            <Stat label="Latest decision" value={ov.latest_run ? <GateBadge outcome={ov.latest_run.gate_outcome} status={ov.latest_run.status} /> : "No runs yet"} hint={ov.latest_run ? formatDate(ov.latest_run.created_at) : undefined} />
            {(["PASS", "FAIL", "BLOCKED", "INCOMPLETE"] as const).map((k) => <Stat key={k} label={k} value={ov.gate_counts[k]} hint="verification runs" />)}
          </div>
          {ov.active_run && <Notice tone="info"><Link className="underline" to={`/repositories/${repositoryId}/verification/${ov.active_run.id}`}>A run is {ov.active_run.status}</Link>. Its decision is not available until it finishes.</Notice>}
          {ov.latest_baseline ? <p className="mt-2 text-sm">Latest baseline: <strong>{ov.latest_baseline.label || ov.latest_baseline.id.slice(0, 8)}</strong> at <code>{shortSha(ov.latest_baseline.head_sha)}</code>, {ov.latest_baseline.totals.passed ?? 0} passed / {ov.latest_baseline.totals.failed ?? 0} failed.</p>
            : <Notice tone="warning">No baseline recorded. Failures cannot be classified as new or pre-existing without one.</Notice>}
        </Card>
      )}
      <Card title="Verify a change">
        <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); start.mutate(); }}>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Change source" htmlFor="cv-source"><select id="cv-source" value={source} onChange={(e) => { setSource(e.target.value as CvSource); setPreview(null); }} className="w-full rounded border border-slate-300 p-1.5 text-sm">{Object.entries(SOURCES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></Field>
            <Field label="Mode" htmlFor="cv-mode" hint="Empty uses the policy default.">
              <select id="cv-mode" value={mode} onChange={(e) => setMode(e.target.value as "" | CvMode)} className="w-full rounded border border-slate-300 p-1.5 text-sm"><option value="">Policy default</option>{["focused", "standard", "full"].map((m) => <option key={m}>{m}</option>)}</select>
            </Field>
          </div>
          {source === "revisions" && <div className="grid gap-3 sm:grid-cols-2"><Field label="Base revision" htmlFor="cv-base"><TextInput id="cv-base" value={base} onChange={(e) => setBase(e.target.value)} placeholder="e.g. main" /></Field><Field label="Head revision" htmlFor="cv-head"><TextInput id="cv-head" value={head} onChange={(e) => setHead(e.target.value)} placeholder="e.g. HEAD" /></Field></div>}
          {source === "manifest" && <Field label="Paths (one per line)" htmlFor="cv-paths"><TextArea id="cv-paths" rows={4} value={paths} onChange={(e) => setPaths(e.target.value)} /></Field>}
          {source === "patch" && <Field label="Unified diff" htmlFor="cv-patch" hint="It is inspected only; NOVA never applies it."><TextArea id="cv-patch" rows={6} value={patch} onChange={(e) => setPatch(e.target.value)} /></Field>}
          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="secondary" disabled={doPreview.isPending} onClick={() => doPreview.mutate()}>{doPreview.isPending ? "Planning…" : "Preview plan (runs nothing)"}</Button>
            <Button type="submit" disabled={start.isPending}>{start.isPending ? "Starting…" : "Run verification"}</Button>
          </div>
          <p className="text-xs text-slate-600">Running executes the repository's own tests through NOVA's restricted runner (not a sandbox). Output is bounded and secrets are redacted.</p>
          <MutationError error={doPreview.error ?? start.error} />
        </form>
        {preview && <PlanView preview={preview} />}
      </Card>
      <Card title="Baselines">
        <form className="mb-3 flex flex-wrap items-end gap-2" onSubmit={(e) => { e.preventDefault(); rec.mutate(); }}>
          <Field label="Label" htmlFor="cv-label"><TextInput id="cv-label" value={label} onChange={(e) => setLabel(e.target.value)} maxLength={120} placeholder="e.g. before refactor" /></Field>
          <Button type="submit" variant="secondary" disabled={rec.isPending}>{rec.isPending ? "Recording…" : "Record baseline (runs all tests)"}</Button>
        </form>
        <MutationError error={rec.error} />
        {baselines.isLoading ? <Spinner /> : baselines.isError ? <ErrorState error={baselines.error} /> : baselines.data!.length === 0 ? <EmptyState title="No baselines">Record one from a state you trust.</EmptyState>
          : <ul className="divide-y divide-slate-200" aria-label="Baselines">{baselines.data!.map((b) => <BaselineRow key={b.id} b={b} />)}</ul>}
      </Card>
      {policy.isLoading ? <Spinner label="Loading policy…" /> : policy.isError ? <ErrorState error={policy.error} /> : policy.data && <PolicyCard key={policy.data.version} repositoryId={repositoryId} policy={policy.data} />}
      <Card title="Run history">
        <div className="mb-2"><label className="text-sm">Outcome <select aria-label="Outcome filter" value={outcome} onChange={(e) => { setOutcome(e.target.value); setPage(1); }} className="ml-1 rounded border border-slate-300 p-1 text-sm"><option value="">All</option>{["pass", "fail", "blocked", "incomplete"].map((o) => <option key={o} value={o}>{o.toUpperCase()}</option>)}</select></label></div>
        {runs.isLoading ? <Spinner /> : runs.isError ? <ErrorState error={runs.error} onRetry={() => void runs.refetch()} /> : runs.data!.items.length === 0 ? <EmptyState title={outcome ? "No runs with this outcome" : "No verification runs yet"}>Preview a change, then run verification.</EmptyState> : (
          <>
            <ul className="divide-y divide-slate-200" aria-label="Verification runs">
              {runs.data!.items.map((r) => (
                <li key={r.id} className="flex flex-wrap items-center gap-2 py-2 text-sm">
                  <GateBadge outcome={r.gate_outcome} status={r.status} />
                  <Link className="font-medium underline" to={`/repositories/${repositoryId}/verification/${r.id}`}>{r.source.replace("_", " ")} · {r.mode}</Link>
                  <span className="text-xs text-slate-500">{r.files_changed} file(s) · <code>{shortSha(r.head_sha)}</code> · {formatDate(r.created_at)}</span>
                  {r.reason && <span className="text-xs text-slate-600">{r.reason}</span>}
                </li>
              ))}
            </ul>
            <Pagination page={runs.data!.page} pages={runs.data!.pages} total={runs.data!.total} onPage={setPage} />
          </>
        )}
      </Card>
    </>
  );
}
