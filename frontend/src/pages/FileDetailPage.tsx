import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { Badge, ButtonLink, Card, EmptyState, ErrorState, Notice, PageHeader, Spinner, statusTone } from "../components/ui";
import { api } from "../services/api";
import { ANALYSIS_LABELS, formatBytes, RESOLUTION_LABELS } from "../services/format";

export function FileDetailPage() {
  const { repositoryId = "", fileId = "" } = useParams();
  const detail = useQuery({ queryKey: ["file", repositoryId, fileId], queryFn: () => api.fileDetail(repositoryId, fileId) });
  const symbols = useQuery({ queryKey: ["file-symbols", repositoryId, fileId], queryFn: () => api.fileSymbols(repositoryId, fileId), enabled: detail.isSuccess });
  const imports = useQuery({ queryKey: ["file-imports", repositoryId, fileId], queryFn: () => api.fileImports(repositoryId, fileId), enabled: detail.isSuccess });
  const deps = useQuery({ queryKey: ["file-deps", repositoryId, fileId], queryFn: () => api.fileDependencies(repositoryId, fileId), enabled: detail.isSuccess });
  const dependents = useQuery({ queryKey: ["file-dependents", repositoryId, fileId], queryFn: () => api.fileDependents(repositoryId, fileId), enabled: detail.isSuccess });

  if (detail.isPending) return <Spinner />;
  if (detail.isError) {
    return (
      <>
        <ErrorState error={detail.error} onRetry={() => void detail.refetch()} />
        <p className="mt-3 text-sm text-slate-600">
          File identifiers change when the repository is re-indexed. <Link className="text-sky-700 hover:underline" to={`/repositories/${repositoryId}`}>Go back to the repository</Link> and open the file again.
        </p>
      </>
    );
  }
  const f = detail.data;

  return (
    <>
      <PageHeader
        title={f.rel_path}
        subtitle={<>{f.language} · {f.line_count.toLocaleString()} lines · {formatBytes(f.size_bytes)} · index generation {f.index.generation}</>}
        actions={<ButtonLink to={`/repositories/${repositoryId}`} variant="secondary">Back to repository</ButtonLink>}
      />
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Badge tone={statusTone(f.analysis_status)}>{ANALYSIS_LABELS[f.analysis_status] ?? f.analysis_status}</Badge>
        {f.parser && <Badge>{f.parser}</Badge>}
        {f.language_confidence !== "high" && <Badge tone="warning">language guess: {f.language_confidence} confidence</Badge>}
        <span className="font-mono text-xs text-slate-500">sha256 {f.sha256.slice(0, 12)}</span>
      </div>
      {f.analysis_status === "unsupported" && (
        <div className="mb-4"><Notice tone="info">NOVA has no structural parser for {f.language}. This file is indexed as metadata only, so it has no symbols or import analysis.</Notice></div>
      )}
      {f.analysis_status === "unavailable" && (
        <div className="mb-4"><Notice tone="warning">A parser exists for {f.language} but is not available in this installation. Symbols are missing, not absent.</Notice></div>
      )}
      {f.parse_diagnostics.length > 0 && (
        <div className="mb-4">
          <Notice tone={f.analysis_status === "failed" ? "danger" : "warning"}>
            <ul className="list-disc pl-5">{f.parse_diagnostics.map((d, i) => <li key={i}>{d}</li>)}</ul>
          </Notice>
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title={`Symbols (${f.symbol_count})`}>
          {symbols.isPending && <Spinner />}
          {symbols.isError && <ErrorState error={symbols.error} />}
          {symbols.isSuccess && symbols.data.items.length === 0 && <p className="text-sm text-slate-600">No symbols extracted.</p>}
          {symbols.isSuccess && symbols.data.items.length > 0 && (
            <ul className="space-y-1.5 text-sm">
              {symbols.data.items.map((s) => (
                <li key={s.id} className="flex flex-wrap items-baseline justify-between gap-2">
                  <span><span className="font-medium">{s.name}</span> <Badge>{s.kind}</Badge>{s.exported && <> <Badge tone="info">exported</Badge></>}</span>
                  <span className="font-mono text-xs text-slate-500">line {s.start_line}{s.end_line !== s.start_line ? `–${s.end_line}` : ""}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title={`Imports (${f.import_count})`}>
          {imports.isPending && <Spinner />}
          {imports.isError && <ErrorState error={imports.error} />}
          {imports.isSuccess && imports.data.items.length === 0 && <p className="text-sm text-slate-600">No imports recorded.</p>}
          {imports.isSuccess && imports.data.items.length > 0 && (
            <ul className="space-y-2 text-sm">
              {imports.data.items.map((i) => (
                <li key={i.id}>
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <span className="font-mono text-xs">{i.specifier || "(local export list)"}</span>
                    <Badge tone={statusTone(i.resolution_status === "resolved" ? "completed" : i.resolution_status)}>{RESOLUTION_LABELS[i.resolution_status] ?? i.resolution_status}</Badge>
                  </div>
                  <div className="text-xs text-slate-500">
                    line {i.line}{i.resolved_path ? ` → ${i.resolved_path}` : ""}{i.package ? ` · package ${i.package}` : ""}{i.resolution_note ? ` · ${i.resolution_note}` : ""}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title={`Depends on (${f.dependency_count})`}>
          {deps.isSuccess && deps.data.items.length === 0 && <p className="text-sm text-slate-600">No recorded dependencies.</p>}
          {deps.isSuccess && deps.data.items.length > 0 && (
            <ul className="space-y-1.5 text-sm">
              {deps.data.items.map((d, i) => (
                <li key={i} className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="font-mono text-xs">{d.path ?? d.target_text}</span>
                  <span className="text-xs text-slate-500">{d.type.replace(/_/g, " ").toLowerCase()} · {d.evidence}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title={`Depended on by (${f.dependent_count})`}>
          {dependents.isSuccess && dependents.data.items.length === 0 && (
            <EmptyState title="No dependents found">No other indexed file imports this one. Dynamic or unsupported import forms are not detected.</EmptyState>
          )}
          {dependents.isSuccess && dependents.data.items.length > 0 && (
            <ul className="space-y-1.5 text-sm">
              {dependents.data.items.map((d, i) => (
                <li key={i} className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="font-mono text-xs">{d.path}</span>
                  <span className="text-xs text-slate-500">{d.type.replace(/_/g, " ").toLowerCase()} · {d.evidence}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </>
  );
}
