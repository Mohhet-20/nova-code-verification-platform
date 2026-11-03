import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { useParams } from "react-router-dom";
import { Badge, ButtonLink, Card, ErrorState, Notice, PageHeader, Spinner, statusTone } from "../components/ui";
import { api } from "../services/api";
import { ANALYSIS_LABELS, formatBytes, formatDate, isPublished, SKIP_LABELS } from "../services/format";

export function IndexingPage() {
  const { repositoryId = "", snapshotId = "" } = useParams();
  const qc = useQueryClient();
  const snap = useQuery({
    queryKey: ["snapshot", repositoryId, snapshotId],
    queryFn: () => api.getSnapshot(repositoryId, snapshotId),
    refetchInterval: (q) => (q.state.data?.status === "running" ? 1000 : false),
  });
  const status = snap.data?.status;
  const diagnostics = useQuery({
    queryKey: ["diagnostics", repositoryId, snapshotId],
    queryFn: () => api.snapshotDiagnostics(repositoryId, snapshotId),
    enabled: isPublished(status),
  });

  useEffect(() => {
    if (isPublished(status) || status === "failed") {
      void qc.invalidateQueries({ queryKey: ["repository", repositoryId] });
      void qc.invalidateQueries({ queryKey: ["summary", repositoryId] });
      void qc.invalidateQueries({ queryKey: ["files", repositoryId] });
      void qc.invalidateQueries({ queryKey: ["freshness", repositoryId] });
      void qc.invalidateQueries({ queryKey: ["symbol-search", repositoryId] });
      void qc.invalidateQueries({ queryKey: ["overview"] });
      void qc.invalidateQueries({ queryKey: ["repositories"] });
    }
  }, [status, qc, repositoryId]);

  if (snap.isPending) return <Spinner label="Loading indexing status…" />;
  if (snap.isError) return <ErrorState error={snap.error} onRetry={() => void snap.refetch()} />;
  const s = snap.data;

  return (
    <>
      <PageHeader
        title="Repository indexing"
        subtitle={<>Snapshot <span className="font-mono text-xs">{s.id.slice(0, 8)}</span> started {formatDate(s.started_at)}</>}
        actions={<ButtonLink to={`/repositories/${repositoryId}`} variant="secondary">Back to repository</ButtonLink>}
      />
      <div className="mb-4 flex items-center gap-3">
        <Badge tone={statusTone(s.status)}>{s.status}</Badge>
        {s.status === "running" && (
          <span role="status" className="flex items-center gap-2 text-sm text-slate-600">
            <span className="h-3 w-3 animate-spin rounded-full border-2 border-slate-300 border-t-sky-700" />
            Scanning… {s.files_indexed.toLocaleString()} files so far
          </span>
        )}
        {s.finished_at && <span className="text-sm text-slate-500">finished {formatDate(s.finished_at)}</span>}
      </div>
      {s.status === "failed" && <Notice tone="danger">Indexing failed: {s.error_message ?? "unknown error"}</Notice>}
      {s.status === "completed_with_warnings" && s.warnings.length > 0 && (
        <div className="mb-4">
          <Notice tone="warning">
            Completed with warnings. The index is usable but incomplete in the ways listed:
            <ul className="mt-1 list-disc pl-5">{s.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
          </Notice>
        </div>
      )}
      {isPublished(s.status) && s.truncated && (
        <div className="mb-4"><Notice tone="warning">Partial index — stopped because of a limit: {s.truncation_reason}. Raise the limit in the backend configuration if you need the full repository.</Notice></div>
      )}
      {s.status !== "failed" && (
        <div className="grid gap-6 lg:grid-cols-2">
          <Card title="Result">
            <dl className="grid grid-cols-2 gap-4 text-sm">
              <Item label="Files indexed" value={s.files_indexed.toLocaleString()} />
              <Item label="Directories visited" value={s.directories_visited.toLocaleString()} />
              <Item label="Total size" value={formatBytes(s.total_bytes)} />
              <Item label="Total lines" value={s.total_lines.toLocaleString()} />
              <Item label="Symbols" value={s.symbols_count.toLocaleString()} />
              <Item label="Imports (unresolved)" value={`${s.imports_total.toLocaleString()} (${s.imports_unresolved.toLocaleString()})`} />
              <Item label="Relationships" value={s.relationships_count.toLocaleString()} />
              <Item label="Generation" value={`${s.generation} · ${s.mode}`} />
              {s.duration_ms !== null && <Item label="Duration" value={`${(s.duration_ms / 1000).toFixed(1)} s`} />}
              {s.git_branch && <Item label="Branch" value={s.git_branch} />}
              {s.git_head && <Item label="HEAD" value={s.git_head.slice(0, 12)} />}
            </dl>
          </Card>
          <Card title="Skipped by safety rules">
            {Object.keys(s.skipped_counts).length === 0 ? (
              <p className="text-sm text-slate-600">{s.status === "running" ? "Counts appear when the scan completes." : "Nothing was skipped."}</p>
            ) : (
              <ul className="space-y-1.5 text-sm">
                {Object.entries(s.skipped_counts).map(([k, n]) => (
                  <li key={k} className="flex justify-between"><span>{SKIP_LABELS[k] ?? k}</span><span className="tabular-nums text-slate-600">{n}</span></li>
                ))}
              </ul>
            )}
            <p className="mt-3 text-xs text-slate-500">
              Limits: {Object.entries(s.limits).map(([k, v]) => `${k.replace("max_", "max ").replace(/_/g, " ")} ${v.toLocaleString()}`).join(" · ")}
            </p>
          </Card>
          {isPublished(s.status) && (
            <Card title="Changes since previous generation">
              <dl className="grid grid-cols-2 gap-4 text-sm">
                <Item label="New" value={s.files_new.toLocaleString()} />
                <Item label="Changed" value={s.files_changed.toLocaleString()} />
                <Item label="Unchanged" value={s.files_unchanged.toLocaleString()} />
                <Item label="Removed" value={s.files_removed === null ? "unknown (partial scan)" : s.files_removed.toLocaleString()} />
              </dl>
            </Card>
          )}
          {isPublished(s.status) && (
            <Card title="Analysis coverage">
              <ul className="flex flex-wrap gap-2" aria-label="Analysis status counts">
                {Object.entries(s.analysis_counts).map(([k, n]) => (<li key={k}><Badge tone={statusTone(k)}>{ANALYSIS_LABELS[k] ?? k} · {n}</Badge></li>))}
              </ul>
              <p className="mt-3 text-xs text-slate-500">“Metadata only” languages are detected and counted but have no structural parser.</p>
            </Card>
          )}
          {diagnostics.isSuccess && diagnostics.data.items.length > 0 && (
            <Card title={`Diagnostics (${diagnostics.data.total})`} className="lg:col-span-2">
              <ul className="max-h-72 space-y-1 overflow-y-auto text-xs">
                {diagnostics.data.items.map((d, i) => (
                  <li key={i} className="flex flex-wrap gap-x-3">
                    <Badge tone={d.severity === "error" ? "danger" : d.severity === "warning" ? "warning" : "neutral"}>{d.code}</Badge>
                    <span className="font-mono">{d.rel_path ?? "(path withheld)"}</span>
                    {d.detail && <span className="text-slate-500">{d.detail}</span>}
                  </li>
                ))}
              </ul>
            </Card>
          )}
          {Object.keys(s.language_counts).length > 0 && (
            <Card title="Languages" className="lg:col-span-2">
              <ul className="flex flex-wrap gap-2">
                {Object.entries(s.language_counts).sort((a, b) => b[1] - a[1]).map(([l, n]) => (
                  <li key={l}><Badge>{l} · {n}</Badge></li>
                ))}
              </ul>
            </Card>
          )}
        </div>
      )}
    </>
  );
}

function Item({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className="text-base font-semibold tabular-nums">{value}</dd>
    </div>
  );
}
