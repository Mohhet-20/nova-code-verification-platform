import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import type { ReactNode } from "react";
import { useParams } from "react-router-dom";
import { Badge, Button, ButtonLink, Card, EmptyState, ErrorState, Notice, PageHeader, Pagination, Spinner, Stat, TextInput } from "../components/ui";
import { ApiError, api, describeError } from "../services/api";
import { formatDate } from "../services/format";
import type { TestCandidate, TestExecutionRun, TestGeneration } from "../types/api";

const TABS = ["Overview", "Discovery", "Coverage", "Generate", "Runs"] as const;
type Tab = (typeof TABS)[number];
const RUN_TONE: Record<string, "success" | "danger" | "warning" | "info" | "neutral"> = {
  passed: "success", failed: "danger", timed_out: "warning", rejected: "danger", error: "danger", running: "info", pending: "info",
};
const CAND_TONE: Record<string, "success" | "danger" | "warning" | "info" | "neutral"> = {
  generated: "neutral", validated: "info", approved: "success", rejected: "danger",
};
const pct = (n: number | null) => (n === null ? "—" : `${n.toFixed(1)}%`);

/** Permission problems get their own wording; everything else uses the shared error text. */
function MutationError({ error }: { error: unknown }) {
  if (error instanceof ApiError && error.status === 403)
    return <Notice tone="warning">Your role does not permit this action. You can still read results.</Notice>;
  return <Notice tone="danger">{describeError(error)}</Notice>;
}

export function TestingPage() {
  const { repositoryId = "" } = useParams();
  const [tab, setTab] = useState<Tab>("Overview");
  const [selectedGen, setSelectedGen] = useState<string | null>(null);
  const repo = useQuery({ queryKey: ["repository", repositoryId], queryFn: () => api.getRepository(repositoryId) });
  return (
    <>
      <PageHeader
        title="Tests & coverage"
        subtitle={repo.data ? `Test discovery, generation and measured coverage for ${repo.data.name}. Nothing runs without your approval.` : undefined}
        actions={<ButtonLink to={`/repositories/${repositoryId}`} variant="secondary">Back to repository</ButtonLink>}
      />
      <div role="tablist" aria-label="Testing sections" className="mb-5 flex flex-wrap gap-1 border-b border-slate-200">
        {TABS.map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}
            className={`px-4 py-2 text-sm font-medium ${tab === t ? "border-b-2 border-sky-700 text-sky-800" : "text-slate-600 hover:text-slate-900"}`}>
            {t}
          </button>
        ))}
      </div>
      {tab === "Overview" && <OverviewTab id={repositoryId} />}
      {tab === "Discovery" && <DiscoveryTab id={repositoryId} />}
      {tab === "Coverage" && <CoverageTab id={repositoryId} onGenerate={() => setTab("Generate")} />}
      {tab === "Generate" && <GenerateTab id={repositoryId} selected={selectedGen} onSelect={setSelectedGen} onRan={() => setTab("Runs")} />}
      {tab === "Runs" && <RunsTab id={repositoryId} />}
    </>
  );
}

function useInvalidate() {
  const qc = useQueryClient();
  return () => {
    for (const k of ["tests-overview", "test-files", "generations", "generation", "test-runs", "coverage", "coverage-files", "coverage-gaps"])
      void qc.invalidateQueries({ queryKey: [k] });
  };
}

function OverviewTab({ id }: { id: string }) {
  const q = useQuery({ queryKey: ["tests-overview", id], queryFn: () => api.testsOverview(id) });
  const cov = useQuery({ queryKey: ["coverage", id], queryFn: () => api.coverage(id) });
  const inv = useInvalidate();
  const discover = useMutation({ mutationFn: () => api.discoverTests(id), onSuccess: inv });
  if (q.isPending) return <Spinner label="Loading test overview…" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  const d = q.data.discovery;
  return (
    <section aria-label="Testing overview" className="space-y-5">
      <Notice tone="info">{q.data.note}</Notice>
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Stat label="Tests discovered" value={d ? d.tests_found : "—"} hint={d ? `${d.files_found} test file(s)` : "Not discovered yet"} />
        <Stat label="Source files with a matching import" value={d ? `${d.source_files_with_tests}/${d.source_files_total}` : "—"} hint="By import, not file name" />
        <Stat label="Generation runs" value={q.data.generation_runs} />
        <Stat label="Measured line coverage" value={cov.data?.state === "measured" ? pct(cov.data.run?.line_percent ?? null) : "Not measured"}
          hint={cov.data?.state === "measured" ? cov.data.basis ?? undefined : "Only shown after an approved run measures it"} />
      </div>
      <div className="flex items-center gap-3">
        <Button onClick={() => discover.mutate()} disabled={discover.isPending}>{discover.isPending ? "Discovering…" : d ? "Re-run discovery" : "Discover tests"}</Button>
        {d && <span className="text-sm text-slate-600">Last discovery {formatDate(d.finished_at ?? d.started_at)}</span>}
      </div>
      {discover.isError && <MutationError error={discover.error} />}
      {!d && <EmptyState title="No discovery yet">Discovery reads test files statically. It does not import or run anything.</EmptyState>}
    </section>
  );
}

function DiscoveryTab({ id }: { id: string }) {
  const [page, setPage] = useState(1);
  const ov = useQuery({ queryKey: ["tests-overview", id], queryFn: () => api.testsOverview(id) });
  const q = useQuery({ queryKey: ["test-files", id, page], queryFn: () => api.testFiles(id, page) });
  if (q.isPending || ov.isPending) return <Spinner label="Loading discovered tests…" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  const d = ov.data?.discovery;
  if (!d) return <EmptyState title="Tests have not been discovered">Use “Discover tests” on the Overview tab.</EmptyState>;
  return (
    <div className="space-y-4">
      <Notice tone="info">Discovered tests were found statically. Discovered does not mean executed or passing.</Notice>
      {d.errors.length > 0 && (
        <Notice tone="warning">
          <ul className="list-disc pl-5">{d.errors.map((e) => <li key={e}>{e}</li>)}</ul>
        </Notice>
      )}
      {q.data.items.length === 0 ? <EmptyState title="No test files found" /> : (
        <ul className="divide-y divide-slate-200 rounded-lg border border-slate-200 bg-white">
          {q.data.items.map((f) => (
            <li key={f.id} className="p-4">
              <div className="flex items-center gap-2">
                <span className="font-mono text-sm">{f.rel_path}</span>
                <Badge tone={f.parse_status === "ok" ? "success" : "danger"}>{f.parse_status === "ok" ? `${f.test_count} test(s)` : f.parse_status}</Badge>
              </div>
              {f.parse_error && <p className="mt-1 text-sm text-red-800">{f.parse_error}</p>}
              {f.associated_sources.length > 0 && (
                <p className="mt-1 text-sm text-slate-600">
                  Imports: {f.associated_sources.map((a) => `${a.path} (${a.via})`).join(", ")}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
      <Pagination page={q.data.page} pages={q.data.pages} total={q.data.total} onPage={setPage} />
    </div>
  );
}

function CoverageTab({ id, onGenerate }: { id: string; onGenerate: () => void }) {
  const [filesPage, setFilesPage] = useState(1);
  const [gapsPage, setGapsPage] = useState(1);
  const [confirm, setConfirm] = useState(false);
  const inv = useInvalidate();
  const cov = useQuery({ queryKey: ["coverage", id], queryFn: () => api.coverage(id) });
  const files = useQuery({ queryKey: ["coverage-files", id, filesPage], queryFn: () => api.coverageFiles(id, filesPage) });
  const gaps = useQuery({ queryKey: ["coverage-gaps", id, gapsPage], queryFn: () => api.coverageGaps(id, gapsPage) });
  const run = useMutation({ mutationFn: () => api.executeExisting(id, true), onSuccess: () => { setConfirm(false); inv(); } });
  if (cov.isPending) return <Spinner label="Loading coverage…" />;
  if (cov.isError) return <ErrorState error={cov.error} onRetry={() => void cov.refetch()} />;
  const r = cov.data.run;
  return (
    <div className="space-y-6">
      <Card title="Measured coverage">
        {cov.data.state === "never_measured" || !r ? (
          <EmptyState title="Coverage has not been measured">{cov.data.message}</EmptyState>
        ) : (
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
              <Stat label="Line coverage" value={pct(r.line_percent)} hint={`${r.lines_covered}/${r.lines_total} lines`} />
              <Stat label="Branch coverage" value={pct(r.branch_percent)} hint={`${r.branches_covered}/${r.branches_total} branches`} />
              <Stat label="Tool" value={r.coverage_tool ?? "—"} />
              <Stat label="Measured" value={formatDate(r.finished_at)} />
            </div>
            <Notice tone="info">Basis: {cov.data.basis}. {cov.data.message}</Notice>
            {cov.data.unmeasured_targets.length > 0 && (
              <Notice tone="warning">Not measured (no data): {cov.data.unmeasured_targets.join(", ")}</Notice>
            )}
          </div>
        )}
        <div className="mt-4 space-y-2 border-t border-slate-200 pt-4">
          <label className="flex items-start gap-2 text-sm">
            <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} className="mt-1" />
            <span>I understand this runs the repository’s own test suite on this machine. It is a restricted subprocess, not a sandbox.</span>
          </label>
          <Button disabled={!confirm || run.isPending} onClick={() => run.mutate()}>{run.isPending ? "Starting…" : "Run existing suite with coverage"}</Button>
          {run.isError && <MutationError error={run.error} />}
        </div>
      </Card>
      <Card title="Files">
        {files.isPending ? <Spinner /> : files.isError ? <ErrorState error={files.error} /> : files.data.items.length === 0 ? (
          <EmptyState title="No per-file data">Per-file numbers appear only after a measured run.</EmptyState>
        ) : (
          <>
            <table className="w-full text-left text-sm">
              <thead><tr className="text-slate-500"><th className="py-1">File</th><th>Lines</th><th>Covered</th><th>Missing</th></tr></thead>
              <tbody>
                {files.data.items.map((f) => (
                  <tr key={f.rel_path} className="border-t border-slate-100">
                    <td className="py-1 font-mono">{f.rel_path}</td>
                    <td>{f.statements}</td>
                    <td>{pct(f.line_percent)}</td>
                    <td className="font-mono text-xs">{f.missing_lines.map(([a, b]) => (a === b ? a : `${a}-${b}`)).join(", ") || "none"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <Pagination page={files.data.page} pages={files.data.pages} total={files.data.total} onPage={setFilesPage} />
          </>
        )}
      </Card>
      <Card title="Coverage gaps">
        {gaps.isPending ? <Spinner /> : gaps.isError ? <ErrorState error={gaps.error} /> : (
          <>
            <Notice tone="info">{gaps.data.note}</Notice>
            {gaps.data.items.length === 0 ? <EmptyState title="No gaps to report" /> : (
              <ul className="mt-3 divide-y divide-slate-200">
                {gaps.data.items.map((g) => (
                  <li key={`${g.rel_path}:${g.symbol}`} className="py-3 text-sm">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono">{g.rel_path}::{g.symbol}</span>
                      <Badge tone={g.evidence === "static" ? "neutral" : "info"}>{g.evidence === "static" ? "Static recommendation" : "Measured + static"}</Badge>
                      <Badge>{g.confidence} confidence</Badge>
                    </div>
                    <p className="text-slate-600">{g.reasons.join("; ")}</p>
                    <p className="text-xs text-slate-500">{g.limitations}</p>
                  </li>
                ))}
              </ul>
            )}
            <Pagination page={gaps.data.page} pages={Math.max(1, Math.ceil(gaps.data.total / gaps.data.page_size))} total={gaps.data.total} onPage={setGapsPage} />
            <Button variant="secondary" className="mt-2" onClick={onGenerate}>Go to generation</Button>
          </>
        )}
      </Card>
    </div>
  );
}

function GenerateTab({ id, selected, onSelect, onRan }: { id: string; selected: string | null; onSelect: (g: string | null) => void; onRan: () => void }) {
  const [path, setPath] = useState("");
  const [symbol, setSymbol] = useState("");
  const inv = useInvalidate();
  const gens = useQuery({ queryKey: ["generations", id], queryFn: () => api.generations(id) });
  const plan = useMutation({ mutationFn: () => api.planTests(id, path.trim(), symbol.trim()) });
  const gen = useMutation({ mutationFn: () => api.generateTests(id, path.trim(), symbol.trim()), onSuccess: (g) => { onSelect(g.id); inv(); } });
  return (
    <div className="space-y-6">
      <Card title="Plan or generate tests">
        <form className="flex flex-wrap items-end gap-3" onSubmit={(e) => e.preventDefault()}>
          <label className="text-sm">Source file
            <TextInput value={path} onChange={(e) => setPath(e.target.value)} placeholder="pkg/calc.py" aria-label="Source file path" />
          </label>
          <label className="text-sm">Function (optional)
            <TextInput value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder="percent" aria-label="Function name" />
          </label>
          <Button variant="secondary" disabled={!path.trim() || plan.isPending} onClick={() => plan.mutate()}>Preview plan</Button>
          <Button disabled={!path.trim() || gen.isPending} onClick={() => gen.mutate()}>{gen.isPending ? "Generating…" : "Generate candidates"}</Button>
        </form>
        <p className="mt-2 text-xs text-slate-600">Generating writes files to a staging area outside your repository. It does not run them.</p>
        {plan.isError && <MutationError error={plan.error} />}
        {gen.isError && <MutationError error={gen.error} />}
        {plan.data && (
          <div className="mt-3 space-y-2">
            <Notice tone="info">{plan.data.note}</Notice>
            <pre className="max-h-64 overflow-auto rounded bg-slate-50 p-3 text-xs">{JSON.stringify(plan.data.targets, null, 2)}</pre>
          </div>
        )}
      </Card>
      <Card title="Generation runs">
        {gens.isPending ? <Spinner /> : gens.isError ? <ErrorState error={gens.error} onRetry={() => void gens.refetch()} /> :
          gens.data.items.length === 0 ? <EmptyState title="Nothing generated yet" /> : (
            <ul className="divide-y divide-slate-200">
              {gens.data.items.map((g) => (
                <li key={g.id} className="flex items-center justify-between py-2 text-sm">
                  <span>{g.targets.map((t) => t.path).join(", ")} <span className="text-slate-500">· {formatDate(g.created_at)}</span></span>
                  <span className="flex items-center gap-2"><Badge tone={CAND_TONE[g.status] ?? "neutral"}>{g.status}</Badge>
                    <Button variant="secondary" onClick={() => onSelect(g.id)}>Review</Button></span>
                </li>
              ))}
            </ul>
          )}
      </Card>
      {selected && <ReviewPanel id={id} genId={selected} onRan={onRan} />}
    </div>
  );
}

function ReviewPanel({ id, genId, onRan }: { id: string; genId: string; onRan: () => void }) {
  const inv = useInvalidate();
  const q = useQuery({ queryKey: ["generation", id, genId], queryFn: () => api.generation(id, genId) });
  const lock = useRef(false);
  const act = useMutation({
    mutationFn: (kind: "validate" | "approve") => (kind === "validate" ? api.validateGeneration(id, genId) : api.approveGeneration(id, genId)),
    onSuccess: inv,
    onSettled: () => { lock.current = false; },
  });
  const exec = useMutation({ mutationFn: () => api.executeGeneration(id, genId, true), onSuccess: () => { inv(); onRan(); }, onSettled: () => { lock.current = false; } });
  if (q.isPending) return <Spinner label="Loading generated candidates…" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  const g: TestGeneration = q.data;
  const anyApproved = g.candidates.some((c) => c.status === "approved");
  const once = (fn: () => void) => { if (lock.current) return; lock.current = true; fn(); };
  return (
    <section aria-label="Review generated tests" className="space-y-4">
      <Notice tone="warning">Review the code below before approving. Approving lets NOVA run these files as a restricted subprocess on this machine. That is not a sandbox.</Notice>
      {g.warnings.length > 0 && <Notice tone="warning">{g.warnings.join(" ")}</Notice>}
      {g.candidates.map((c) => <CandidateView key={c.id} c={c} />)}
      <div className="flex flex-wrap gap-2">
        <Button variant="secondary" disabled={act.isPending} onClick={() => once(() => act.mutate("validate"))}>Validate</Button>
        <Button disabled={act.isPending || !g.candidates.some((c) => c.status === "validated")} onClick={() => once(() => act.mutate("approve"))}>Approve validated</Button>
        <Button disabled={!anyApproved || exec.isPending} onClick={() => once(() => exec.mutate())}>{exec.isPending ? "Starting…" : "Run approved tests"}</Button>
      </div>
      {act.isError && <MutationError error={act.error} />}
      {exec.isError && <MutationError error={exec.error} />}
    </section>
  );
}

function CandidateView({ c }: { c: TestCandidate }): ReactNode {
  return (
    <Card title={`${c.target_path}${c.target_symbol ? `::${c.target_symbol}` : ""}`}>
      <div className="mb-2 flex flex-wrap items-center gap-2 text-sm">
        <Badge tone={CAND_TONE[c.status] ?? "neutral"}>{c.status}</Badge>
        <Badge>{c.runnable_count} runnable</Badge>
        {c.review_required && <Badge tone="warning">Needs human review</Badge>}
        <Badge>{c.confidence} confidence</Badge>
      </div>
      {c.rejection_reasons.length > 0 && <Notice tone="danger">Rejected: {c.rejection_reasons.join("; ")}</Notice>}
      {c.validation.length > 0 && (
        <ul className="my-2 text-sm">
          {c.validation.map((v) => <li key={v.check}>{v.passed ? "✔" : "✘"} <strong>{v.check}</strong>: {v.detail}</li>)}
        </ul>
      )}
      <details className="mb-2">
        <summary className="cursor-pointer text-sm font-medium">Planned cases ({c.cases.length})</summary>
        <ul className="mt-2 space-y-2 text-sm">
          {c.cases.map((k) => (
            <li key={k.id}><strong>{k.title}</strong> — {k.category}, {k.confidence} confidence{k.runnable ? "" : ", template (will be skipped)"}.
              <div className="text-slate-600">Expected: {k.expected} Why: {k.rationale}</div></li>
          ))}
        </ul>
      </details>
      {/* Rendered as text, never as HTML, and never editable here. */}
      <pre aria-label="Generated test code (read-only)" className="max-h-96 overflow-auto rounded bg-slate-900 p-3 text-xs text-slate-100">{c.source ?? "Source is not available."}</pre>
    </Card>
  );
}

function RunsTab({ id }: { id: string }) {
  const [sel, setSel] = useState<string | null>(null);
  const q = useQuery({
    queryKey: ["test-runs", id], queryFn: () => api.testRuns(id),
    refetchInterval: (query) => (query.state.data?.items.some((r) => r.status === "pending" || r.status === "running") ? 1500 : false),
  });
  if (q.isPending) return <Spinner label="Loading test runs…" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  const first = q.data.items[0];
  if (!first) return <EmptyState title="No test runs yet">Tests only run after you approve them.</EmptyState>;
  const chosen = q.data.items.find((r) => r.id === sel) ?? first;
  return (
    <div className="grid gap-6 md:grid-cols-[18rem_1fr]">
      <ul className="space-y-1">
        {q.data.items.map((r) => (
          <li key={r.id}>
            <button onClick={() => setSel(r.id)} className={`w-full rounded border px-3 py-2 text-left text-sm ${chosen.id === r.id ? "border-sky-600 bg-sky-50" : "border-slate-200 bg-white"}`}>
              <Badge tone={RUN_TONE[r.status] ?? "neutral"}>{r.status}</Badge> {r.kind === "existing" ? "Repository suite" : "Generated tests"}
              <div className="text-xs text-slate-500">{formatDate(r.created_at)}</div>
            </button>
          </li>
        ))}
      </ul>
      <RunDetail r={chosen} />
    </div>
  );
}

function RunDetail({ r }: { r: TestExecutionRun }) {
  return (
    <section aria-label="Run details" className="space-y-3">
      <div className="flex items-center gap-2"><Badge tone={RUN_TONE[r.status] ?? "neutral"}>{r.status}</Badge>
        {r.exit_code !== null && <span className="text-sm text-slate-600">exit code {r.exit_code}</span>}
        {r.duration_ms !== null && <span className="text-sm text-slate-600">{(r.duration_ms / 1000).toFixed(1)}s</span>}</div>
      {r.reason && <Notice tone="warning">{r.reason}</Notice>}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <Stat label="Collected" value={r.tests_collected ?? "—"} /><Stat label="Passed" value={r.tests_passed ?? "—"} />
        <Stat label="Failed" value={r.tests_failed ?? "—"} /><Stat label="Errors" value={r.tests_errors ?? "—"} /><Stat label="Skipped" value={r.tests_skipped ?? "—"} />
      </div>
      <p className="text-sm">Coverage: <strong>{r.coverage_status === "measured" ? `${pct(r.line_percent)} lines, ${pct(r.branch_percent)} branches` : r.coverage_status.replace("_", " ")}</strong>
        {r.coverage_error ? ` — ${r.coverage_error}` : ""}</p>
      {r.failures.length > 0 && (
        <ul className="space-y-2">{r.failures.map((f) => (
          <li key={f.name} className="rounded border border-red-200 bg-red-50 p-3 text-sm"><strong>{f.name}</strong>
            {f.message && <div>{f.message}</div>}{f.traceback && <pre className="mt-1 max-h-48 overflow-auto text-xs">{f.traceback}</pre>}</li>
        ))}</ul>
      )}
      {(r.stdout_tail || r.stderr_tail) && <pre aria-label="Captured output" className="max-h-64 overflow-auto rounded bg-slate-50 p-3 text-xs">{r.stdout_tail}{r.stderr_tail}</pre>}
      {r.output_truncated && <Notice tone="warning">Output was truncated at the configured limit.</Notice>}
      <Notice tone="info">Executed as a restricted subprocess (timeout, minimal environment, resource limits where the OS supports them). Not a sandbox: network and file system are not isolated.</Notice>
    </section>
  );
}
