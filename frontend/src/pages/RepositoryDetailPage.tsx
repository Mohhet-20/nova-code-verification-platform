import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Badge, Button, ButtonLink, Card, EmptyState, ErrorState, Notice, PageHeader, Pagination, Spinner, statusTone, TextInput } from "../components/ui";
import { SymbolSearch } from "../components/SymbolSearch";
import { api, describeError } from "../services/api";
import { ANALYSIS_LABELS, formatBytes, formatDate, SKIP_LABELS } from "../services/format";

function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function RepositoryDetailPage() {
  const { repositoryId = "" } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [language, setLanguage] = useState("");
  const [analysis, setAnalysis] = useState("");
  const q = useDebounced(search.trim());

  const repo = useQuery({ queryKey: ["repository", repositoryId], queryFn: () => api.getRepository(repositoryId) });
  const summary = useQuery({ queryKey: ["summary", repositoryId], queryFn: () => api.repositorySummary(repositoryId) });
  const files = useQuery({
    queryKey: ["files", repositoryId, page, q, language, analysis],
    queryFn: ({ signal }) => api.listFiles(repositoryId, { page, pageSize: 25, q, language, analysisStatus: analysis }, signal),
    placeholderData: (prev) => prev,
  });

  const freshness = useQuery({
    queryKey: ["freshness", repositoryId],
    queryFn: () => api.freshness(repositoryId),
    enabled: summary.isSuccess && summary.data.indexed,
  });

  const index = useMutation({
    mutationFn: (mode: "incremental" | "full") => api.startIndexing(repositoryId, mode),
    onSuccess: async (snap) => {
      await qc.invalidateQueries({ queryKey: ["repository", repositoryId] });
      void navigate(`/repositories/${repositoryId}/indexing/${snap.id}`);
    },
  });

  if (repo.isPending) return <Spinner />;
  if (repo.isError) return <ErrorState error={repo.error} onRetry={() => void repo.refetch()} />;
  const r = repo.data;
  const snap = summary.data?.snapshot ?? null;
  const running = r.latest_snapshot?.status === "running";

  return (
    <>
      <PageHeader
        title={r.name}
        subtitle={r.description || undefined}
        actions={
          <>
            {snap && <ButtonLink to={`/repositories/${r.id}/intelligence`} variant="secondary">Code intelligence</ButtonLink>}
            {snap && <ButtonLink to={`/repositories/${r.id}/health`} variant="secondary">Code health</ButtonLink>}
            {snap && <ButtonLink to={`/repositories/${r.id}/testing`} variant="secondary">Tests &amp; coverage</ButtonLink>}
            {snap && <ButtonLink to={`/repositories/${r.id}/failures`} variant="secondary">Failures &amp; debugging</ButtonLink>}
            <ButtonLink to={`/repositories/${r.id}/reviews`} variant="secondary">Code review</ButtonLink>
            {snap && <ButtonLink to={`/repositories/${r.id}/repairs`} variant="secondary">Repair intelligence</ButtonLink>}
            {snap && <ButtonLink to={`/repositories/${r.id}/refactors`} variant="secondary">Refactoring</ButtonLink>}
            {snap && <ButtonLink to={`/repositories/${r.id}/verification`} variant="secondary">Verification</ButtonLink>}
            {snap && <ButtonLink to={`/repositories/${r.id}/workflow`} variant="secondary">Workflow center</ButtonLink>}
            {snap && <ButtonLink to={`/repositories/${r.id}/security`} variant="secondary">Security</ButtonLink>}
            <ButtonLink to={`/tasks/new?repository=${r.id}`} variant="secondary">New task</ButtonLink>
            {running ? (
              <ButtonLink to={`/repositories/${r.id}/indexing/${r.latest_snapshot?.id}`}>View indexing</ButtonLink>
            ) : (
              <>
                {snap && (
                  <Button variant="secondary" onClick={() => index.mutate("full")} disabled={index.isPending}>Full re-index</Button>
                )}
                <Button onClick={() => index.mutate("incremental")} disabled={index.isPending}>
                  {index.isPending ? "Starting…" : snap ? "Re-index" : "Index repository"}
                </Button>
              </>
            )}
          </>
        }
      />
      {index.isError && <div className="mb-4"><Notice tone="danger">{describeError(index.error)}</Notice></div>}
      {freshness.isSuccess && freshness.data.state === "stale" && (
        <div className="mb-4">
          <Notice tone="warning">
            The index may be out of date: {freshness.data.changed} changed, {freshness.data.new} new
            {freshness.data.removed !== null ? `, ${freshness.data.removed} removed` : ""} file(s) since the last run
            (metadata check). Re-index to refresh.
          </Notice>
        </div>
      )}
      {freshness.isSuccess && freshness.data.state === "fresh" && (
        <p className="mb-4 text-xs text-slate-500">Index is current with the files on disk (size and modification time check).</p>
      )}

      <div className="grid gap-6 lg:grid-cols-3">
        <Card title="Repository">
          <dl className="space-y-2 text-sm">
            <Row label="Path"><span className="break-all font-mono text-xs">{r.root_path}</span></Row>
            <Row label="Git">{r.is_git ? <Badge tone="info">{r.git_branch ?? "detached"}</Badge> : <Badge>not a git repository</Badge>}</Row>
            {r.is_git && <Row label="HEAD"><span className="font-mono text-xs">{r.git_head?.slice(0, 12) ?? "—"}</span></Row>}
            {r.is_git && <Row label="Uncommitted">{r.git_dirty_count ?? "—"}</Row>}
            <Row label="Registered">{formatDate(r.created_at)}</Row>
          </dl>
        </Card>
        <Card title="Index summary" className="lg:col-span-2">
          {summary.isPending && <Spinner />}
          {summary.isError && <ErrorState error={summary.error} />}
          {summary.isSuccess && !summary.data.indexed && (
            <p className="text-sm text-slate-600">
              This repository has not been indexed yet. Index it to see languages, sizes and a file inventory.
              {r.latest_snapshot?.status === "failed" && (
                <span className="mt-2 block text-red-800">Last attempt failed: {r.latest_snapshot.error_message}</span>
              )}
            </p>
          )}
          {snap && summary.data && (
            <div className="space-y-4 text-sm">
              <div className="flex flex-wrap items-center gap-x-6 gap-y-1">
                <span><strong className="tabular-nums">{snap.files_indexed.toLocaleString()}</strong> files</span>
                <span><strong className="tabular-nums">{snap.total_lines.toLocaleString()}</strong> lines</span>
                <span><strong>{formatBytes(snap.total_bytes)}</strong></span>
                <span className="text-slate-500">indexed {formatDate(snap.finished_at)}</span>
                <Link className="text-sky-700 hover:underline" to={`/repositories/${r.id}/indexing/${snap.id}`}>Details</Link>
              </div>
              {snap.truncated && <Notice tone="warning">Index is partial: {snap.truncation_reason}.</Notice>}
              {snap.status === "completed_with_warnings" && snap.warnings.length > 0 && (
                <Notice tone="warning"><ul className="list-disc pl-5">{snap.warnings.map((w) => <li key={w}>{w}</li>)}</ul></Notice>
              )}
              <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-slate-700">
                <span><strong className="tabular-nums">{snap.symbols_count.toLocaleString()}</strong> symbols</span>
                <span><strong className="tabular-nums">{snap.imports_total.toLocaleString()}</strong> imports ({snap.imports_unresolved.toLocaleString()} unresolved)</span>
                <span>generation <strong className="tabular-nums">{snap.generation}</strong></span>
              </div>
              <ul className="flex flex-wrap gap-2" aria-label="Analysis coverage">
                {Object.entries(snap.analysis_counts).map(([k, n]) => (
                  <li key={k}><Badge tone={statusTone(k)}>{ANALYSIS_LABELS[k] ?? k} · {n}</Badge></li>
                ))}
              </ul>
              <div>
                <p className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">Languages</p>
                <ul className="flex flex-wrap gap-2">
                  {summary.data.top_languages.map(([lang, n]) => (
                    <li key={lang}><Badge>{lang} · {n}</Badge></li>
                  ))}
                </ul>
              </div>
              <div>
                <p className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">Largest files</p>
                <ul className="space-y-0.5">
                  {summary.data.largest_files.map(([p, s]) => (
                    <li key={p} className="flex justify-between gap-3 font-mono text-xs"><span className="truncate">{p}</span><span className="shrink-0 text-slate-500">{formatBytes(s)}</span></li>
                  ))}
                </ul>
              </div>
              {Object.keys(snap.skipped_counts).length > 0 && (
                <p className="text-xs text-slate-500">
                  Skipped: {Object.entries(snap.skipped_counts).map(([k, n]) => `${SKIP_LABELS[k] ?? k} (${n})`).join(" · ")}
                </p>
              )}
            </div>
          )}
        </Card>
      </div>

      <SymbolSearch repositoryId={r.id} indexed={Boolean(summary.data?.indexed)} />

      <h2 className="mb-3 mt-8 text-lg font-semibold">File inventory</h2>
      <p className="mb-3 text-sm text-slate-600">Metadata only. File contents are never shown or sent anywhere.</p>
      <div className="mb-3 flex flex-wrap gap-3">
        <div className="min-w-64 flex-1">
          <label htmlFor="file-search" className="sr-only">Search file paths</label>
          <TextInput id="file-search" type="search" placeholder="Search paths…" value={search} onChange={(e) => { setSearch(e.target.value); setPage(1); }} />
        </div>
        <div>
          <label htmlFor="file-language" className="sr-only">Filter by language</label>
          <select id="file-language" value={language} onChange={(e) => { setLanguage(e.target.value); setPage(1); }}
            className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm">
            <option value="">All languages</option>
            {(summary.data?.snapshot ? Object.keys(summary.data.snapshot.language_counts).sort() : []).map((l) => (
              <option key={l} value={l}>{l}</option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="file-analysis" className="sr-only">Filter by analysis status</label>
          <select id="file-analysis" value={analysis} onChange={(e) => { setAnalysis(e.target.value); setPage(1); }}
            className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm">
            <option value="">Any analysis status</option>
            {Object.entries(ANALYSIS_LABELS).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
          </select>
        </div>
      </div>
      {files.isPending && <Spinner />}
      {files.isError && <ErrorState error={files.error} onRetry={() => void files.refetch()} />}
      {files.isSuccess && files.data.total === 0 && (
        <EmptyState title={summary.data?.indexed ? "No files match" : "Nothing indexed yet"}>
          {summary.data?.indexed ? "Try a different search or language filter." : "Run indexing to populate the inventory."}
        </EmptyState>
      )}
      {files.isSuccess && files.data.total > 0 && (
        <>
          <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white shadow-sm">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Indexed files</caption>
              <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                <tr><th scope="col" className="px-4 py-2.5">Path</th><th scope="col" className="px-4 py-2.5">Language</th><th scope="col" className="px-4 py-2.5">Analysis</th><th scope="col" className="px-4 py-2.5 text-right">Symbols</th><th scope="col" className="px-4 py-2.5 text-right">Lines</th><th scope="col" className="px-4 py-2.5 text-right">Size</th></tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {files.data.items.map((f) => (
                  <tr key={f.id}>
                    <td className="px-4 py-2 font-mono text-xs"><Link className="text-sky-700 hover:underline" to={`/repositories/${r.id}/files/${f.id}`}>{f.rel_path}</Link></td>
                    <td className="px-4 py-2">{f.language}</td>
                    <td className="px-4 py-2"><Badge tone={statusTone(f.analysis_status)}>{ANALYSIS_LABELS[f.analysis_status] ?? f.analysis_status}</Badge></td>
                    <td className="px-4 py-2 text-right tabular-nums">{f.symbol_count}</td>
                    <td className="px-4 py-2 text-right tabular-nums">{f.line_count.toLocaleString()}</td>
                    <td className="px-4 py-2 text-right tabular-nums">{formatBytes(f.size_bytes)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination page={files.data.page} pages={files.data.pages} total={files.data.total} onPage={setPage} />
        </>
      )}
    </>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-4">
      <dt className="text-slate-500">{label}</dt>
      <dd className="text-right text-slate-900">{children}</dd>
    </div>
  );
}
