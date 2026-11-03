import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { Badge, Button, ButtonLink, EmptyState, ErrorState, Notice, PageHeader, Pagination, Spinner, Stat, TextInput } from "../components/ui";
import { api, describeError } from "../services/api";
import { formatDate } from "../services/format";
import type { AnalysisSummary, Finding, FindingFilters, Severity } from "../types/api";

const SEVERITIES: Severity[] = ["critical", "high", "medium", "low", "info"];
const CATEGORIES = ["correctness", "maintainability", "reliability", "security", "complexity"];
const STATUSES = ["new", "existing", "reopened", "suppressed", "resolved"];
const TONE = { critical: "danger", high: "danger", medium: "warning", low: "info", info: "neutral" } as const;
const SELECT = "rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm";

function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function HealthPage() {
  const { repositoryId = "" } = useParams();
  const repo = useQuery({ queryKey: ["repository", repositoryId], queryFn: () => api.getRepository(repositoryId) });
  const [selected, setSelected] = useState<string | null>(null);
  return (
    <>
      <PageHeader
        title="Code health"
        subtitle={repo.data ? `Static analysis findings for ${repo.data.name}. Findings are patterns found by rules, not confirmed defects.` : undefined}
        actions={<ButtonLink to={`/repositories/${repositoryId}`} variant="secondary">Back to repository</ButtonLink>}
      />
      <div className="space-y-6">
        <AnalysisControls repositoryId={repositoryId} />
        <Overview repositoryId={repositoryId} />
        <Explorer repositoryId={repositoryId} selected={selected} onSelect={setSelected} />
      </div>
    </>
  );
}

function AnalysisControls({ repositoryId }: { repositoryId: string }) {
  const qc = useQueryClient();
  const status = useQuery({
    queryKey: ["analysis-status", repositoryId],
    queryFn: () => api.analysisStatus(repositoryId),
    refetchInterval: (q) => (q.state.data?.state === "running" ? 1500 : false),
  });
  const refresh = () => {
    for (const key of ["analysis-status", "analysis-summary", "findings"]) void qc.invalidateQueries({ queryKey: [key] });
  };
  const lock = useRef(false);
  const start = useMutation({
    mutationFn: (mode: "incremental" | "full") => api.startAnalysis(repositoryId, mode),
    onSuccess: refresh,
    onError: refresh,
    onSettled: () => {
      lock.current = false;
    },
  });
  const submit = (mode: "incremental" | "full") => {
    if (lock.current) return; // a second click before React re-renders must not send a second request
    lock.current = true;
    start.mutate(mode);
  };
  const prev = status.data?.state;
  useEffect(() => {
    if (prev && prev !== "running") refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prev]);

  if (status.isPending) return <Spinner label="Loading analysis status…" />;
  if (status.isError) return <ErrorState error={status.error} onRetry={() => void status.refetch()} />;
  const s = status.data;
  const running = s.state === "running" || start.isPending;
  const run = s.latest;
  return (
    <section aria-labelledby="analysis-heading" className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 id="analysis-heading" className="text-sm font-semibold text-slate-800">Analysis</h2>
          <p className="mt-1 flex flex-wrap items-center gap-2 text-sm text-slate-700">
            <Badge tone={s.state === "failed" ? "danger" : s.state === "completed_with_warnings" ? "warning" : s.state === "running" ? "info" : s.state === "completed" ? "success" : "neutral"}>
              {s.state.replace(/_/g, " ")}
            </Badge>
            {run?.finished_at && <span>last run finished {formatDate(run.finished_at)}</span>}
            {s.index_changed_since_run && <span className="text-amber-800">· the index has changed since this run</span>}
          </p>
          <p className="mt-1 text-xs text-slate-500">{s.message}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button onClick={() => submit("incremental")} disabled={running || !s.config.valid}>
            {running ? "Analysing…" : "Run analysis"}
          </Button>
          <Button variant="secondary" onClick={() => submit("full")} disabled={running || !s.config.valid}>Full re-analysis</Button>
        </div>
      </div>
      {!s.config.valid && (
        <div className="mt-3">
          <Notice tone="danger">
            The repository analysis configuration is invalid, so analysis cannot run:
            <ul className="mt-1 list-disc pl-5">{s.config.errors.map((e) => <li key={e}>{e}</li>)}</ul>
          </Notice>
        </div>
      )}
      {start.isError && <div className="mt-3"><Notice tone="danger">{describeError(start.error)}</Notice></div>}
      {s.state === "running" && (
        <p role="status" className="mt-3 text-sm text-slate-600">Analysis in progress. Findings below are from earlier runs.</p>
      )}
      {run?.status === "failed" && run.error_message && <div className="mt-3"><Notice tone="danger">Last run failed: {run.error_message}</Notice></div>}
      {run && run.warnings.length > 0 && (
        <div className="mt-3">
          <Notice tone="warning">
            <ul className="list-disc pl-5">{run.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
          </Notice>
        </div>
      )}
      {run && run.analyzer_errors.length > 0 && (
        <div className="mt-3">
          <Notice tone="warning">
            Some rules failed on some files: {run.analyzer_errors.map((e) => `${e.rule_id} (${e.files} file${e.files === 1 ? "" : "s"})`).join(", ")}. Those files were
            only partly analysed.
          </Notice>
        </div>
      )}
    </section>
  );
}

function Overview({ repositoryId }: { repositoryId: string }) {
  const q = useQuery({ queryKey: ["analysis-summary", repositoryId], queryFn: () => api.analysisSummary(repositoryId) });
  if (q.isPending) return <Spinner label="Loading summary…" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  const s: AnalysisSummary = q.data;
  if (!s.analyzed) {
    return <EmptyState title="Not analysed yet">Use “Run analysis” above. The repository must be indexed first.</EmptyState>;
  }
  return (
    <section aria-label="Code health overview" className="space-y-3">
      <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Open findings" value={s.open} hint={`${s.by_status.suppressed ?? 0} suppressed · ${s.by_status.resolved ?? 0} resolved`} />
        <Stat label="Files analysed" value={s.files_analyzed} hint={s.latest_run ? `${s.latest_run.files_excluded} excluded · ${s.latest_run.files_parse_failed} unparsable` : undefined} />
        <Stat label="Rules enabled" value={s.latest_run?.rules_enabled ?? 0} hint={`${s.latest_run?.rules_disabled ?? 0} disabled`} />
        <Stat label="Rule errors" value={s.analyzer_errors} hint={`${s.warnings} warning${s.warnings === 1 ? "" : "s"}`} />
      </dl>
      <div className="grid gap-3 md:grid-cols-2">
        <Breakdown title="Open by severity" data={SEVERITIES.map((k) => [k, s.by_severity[k] ?? 0] as const)} />
        <Breakdown title="Open by category" data={CATEGORIES.map((k) => [k, s.by_category[k] ?? 0] as const)} />
      </div>
      <ul className="list-disc space-y-0.5 pl-5 text-xs text-slate-500">
        {s.notes.map((n) => <li key={n}>{n}</li>)}
      </ul>
    </section>
  );
}

function Breakdown({ title, data }: { title: string; data: (readonly [string, number])[] }) {
  const max = Math.max(1, ...data.map(([, n]) => n));
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <h3 className="text-sm font-semibold text-slate-800">{title}</h3>
      <ul className="mt-2 space-y-1.5">
        {data.map(([k, n]) => (
          <li key={k} className="flex items-center gap-2 text-sm">
            <span className="w-28 capitalize text-slate-700">{k}</span>
            <span className="h-2 flex-1 overflow-hidden rounded bg-slate-100" aria-hidden="true">
              <span className="block h-2 rounded bg-sky-600" style={{ width: `${(n / max) * 100}%` }} />
            </span>
            <span className="w-8 text-right tabular-nums text-slate-900">{n}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Explorer({ repositoryId, selected, onSelect }: { repositoryId: string; selected: string | null; onSelect: (id: string | null) => void }) {
  const [filters, setFilters] = useState<FindingFilters>({ status: "" });
  const [text, setText] = useState("");
  const q = useDebounced(text);
  const applied = { ...filters, q };
  const list = useQuery({
    queryKey: ["findings", repositoryId, applied],
    queryFn: () => api.findings(repositoryId, applied),
    placeholderData: (prev) => prev,
  });
  const set = (patch: Partial<FindingFilters>) => setFilters((f) => ({ ...f, ...patch, page: 1 }));
  const detail = list.data?.items.find((f) => f.id === selected) ?? null;

  return (
    <section aria-labelledby="findings-heading" className="space-y-3">
      <h2 id="findings-heading" className="text-lg font-semibold text-slate-900">Findings</h2>
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-sm text-slate-700">Search
          <TextInput type="search" value={text} onChange={(e) => { setText(e.target.value); setFilters((f) => ({ ...f, page: 1 })); }} placeholder="Title, path or rule" className="mt-1" />
        </label>
        <label className="text-sm text-slate-700">Severity
          <select className={`${SELECT} mt-1 block`} value={filters.severity ?? ""} onChange={(e) => set({ severity: e.target.value })}>
            <option value="">All</option>{SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <label className="text-sm text-slate-700">Category
          <select className={`${SELECT} mt-1 block`} value={filters.category ?? ""} onChange={(e) => set({ category: e.target.value })}>
            <option value="">All</option>{CATEGORIES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <label className="text-sm text-slate-700">Status
          <select className={`${SELECT} mt-1 block`} value={filters.status ?? ""} onChange={(e) => set({ status: e.target.value })}>
            <option value="">All</option>{STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <label className="text-sm text-slate-700">Sort
          <select className={`${SELECT} mt-1 block`} value={filters.sort ?? "severity"} onChange={(e) => set({ sort: e.target.value })}>
            <option value="severity">Severity</option><option value="last_seen">Last seen</option><option value="path">Path</option><option value="rule">Rule</option>
          </select>
        </label>
      </div>
      {list.isPending && <Spinner label="Loading findings…" />}
      {list.isError && <ErrorState error={list.error} onRetry={() => void list.refetch()} />}
      {list.data && list.data.total === 0 && (
        <EmptyState title="No findings match">
          {filters.severity || filters.category || filters.status || q ? "Try clearing a filter." : "Nothing has been reported. If analysis has not run, use “Run analysis”."}
        </EmptyState>
      )}
      {list.data && list.data.total > 0 && (
        <div className="grid gap-4 lg:grid-cols-[1fr_minmax(0,28rem)]">
          <div>
            <ul className="divide-y divide-slate-200 rounded-lg border border-slate-200 bg-white shadow-sm">
              {list.data.items.map((f) => (
                <li key={f.id}>
                  <button type="button" onClick={() => onSelect(f.id)} aria-pressed={f.id === selected}
                    className={`flex w-full flex-col gap-1 px-4 py-3 text-left hover:bg-slate-50 ${f.id === selected ? "bg-sky-50" : ""}`}>
                    <span className="flex flex-wrap items-center gap-2">
                      <Badge tone={TONE[f.severity]}>{f.severity}</Badge>
                      <Badge>{f.status}</Badge>
                      <span className="text-xs text-slate-500">{f.rule_id}</span>
                    </span>
                    <span className="text-sm font-medium text-slate-900">{f.title}</span>
                    <span className="font-mono text-xs text-slate-600">{f.rel_path}:{f.start_line}</span>
                  </button>
                </li>
              ))}
            </ul>
            <Pagination page={list.data.page} pages={list.data.pages} total={list.data.total} onPage={(p) => setFilters((f) => ({ ...f, page: p }))} />
          </div>
          <FindingDetail key={detail?.id ?? "none"} repositoryId={repositoryId} finding={detail} />
        </div>
      )}
    </section>
  );
}

function FindingDetail({ repositoryId, finding }: { repositoryId: string; finding: Finding | null }) {
  const qc = useQueryClient();
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState<string | null>(null);
  const update = useMutation({
    mutationFn: (body: { status: "suppressed" | "existing"; reason?: string }) => api.updateFinding(repositoryId, finding!.id, body),
    onSuccess: (f) => {
      setReason("");
      setConfirmed(f.status === "suppressed" ? "Finding suppressed." : "Suppression removed; finding is open again.");
      void qc.invalidateQueries({ queryKey: ["findings"] });
      void qc.invalidateQueries({ queryKey: ["analysis-summary"] });
    },
  });
  if (!finding) {
    return <div className="rounded-lg border border-dashed border-slate-300 p-6 text-sm text-slate-600">Select a finding to see what was found, why it matters and what to do.</div>;
  }
  const f = finding;
  return (
    <aside aria-label="Finding details" className="space-y-3 rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={TONE[f.severity]}>{f.severity}</Badge>
        <Badge>{f.status}</Badge>
        <Badge tone="info">{f.confidence} confidence</Badge>
      </div>
      <h3 className="text-base font-semibold text-slate-900">{f.title}</h3>
      <p className="font-mono text-xs text-slate-600">
        {f.file_id ? <Link className="text-sky-800 hover:underline" to={`/repositories/${f.repository_id}/files/${f.file_id}`}>{f.rel_path}</Link> : f.rel_path}
        :{f.start_line}{f.end_line > f.start_line ? `–${f.end_line}` : ""} · {f.rule_id}
      </p>
      <Part title="What was found">{f.explanation}</Part>
      <Part title="Evidence">{f.evidence}{f.metric_value !== null ? ` (measured: ${f.metric_value})` : ""}</Part>
      <Part title="Suggested next step">{f.remediation}</Part>
      <Part title="What this analysis cannot determine">{f.limitations}</Part>
      <p className="text-xs text-slate-500">
        First seen {formatDate(f.first_seen_at)} · last seen {formatDate(f.last_seen_at)}
        {f.reopened_count > 0 ? ` · reopened ${f.reopened_count}×` : ""}
        {f.resolution_reason ? ` · resolved: ${f.resolution_reason.replace(/_/g, " ")}` : ""}
      </p>
      {f.suppression_reason && <Notice tone="info">Suppressed: {f.suppression_reason}</Notice>}
      {confirmed && <p role="status" className="text-sm text-emerald-800">{confirmed}</p>}
      {update.isError && <Notice tone="danger">{describeError(update.error)}</Notice>}
      {f.status === "suppressed" ? (
        <Button variant="secondary" disabled={update.isPending} onClick={() => update.mutate({ status: "existing" })}>
          {update.isPending ? "Saving…" : "Remove suppression"}
        </Button>
      ) : f.status !== "resolved" ? (
        <div className="space-y-2">
          <label className="block text-sm text-slate-700" htmlFor="suppress-reason">Reason for suppressing (required)</label>
          <TextInput id="suppress-reason" value={reason} maxLength={300} onChange={(e) => setReason(e.target.value)} />
          <Button variant="secondary" disabled={update.isPending || reason.trim().length < 5}
            onClick={() => update.mutate({ status: "suppressed", reason: reason.trim() })}>
            {update.isPending ? "Saving…" : "Suppress finding"}
          </Button>
        </div>
      ) : null}
    </aside>
  );
}

function Part({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</h4>
      <p className="mt-0.5 text-sm text-slate-800">{children}</p>
    </div>
  );
}
