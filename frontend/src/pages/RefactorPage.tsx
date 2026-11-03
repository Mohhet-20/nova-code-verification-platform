import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Badge, Button, ButtonLink, Card, EmptyState, ErrorState, Field, Notice, PageHeader, Pagination, Spinner, TextInput } from "../components/ui";
import { api } from "../services/api";
import { formatDate } from "../services/format";
import type { RefactorStatus, RefactorSupport, RefactorType } from "../types/api";
import { MutationError } from "./RepairsPage";

type Tone = "success" | "danger" | "warning" | "info" | "neutral";
export const REFACTOR_TONE: Record<RefactorStatus, Tone> = {
  planned: "neutral", blocked: "danger", generated: "info", invalid: "danger", validated: "info", awaiting_approval: "warning", approved: "info", rejected: "neutral",
  applying: "warning", applied: "warning", verification_failed: "danger", verified: "success", analyzed: "info", closed: "neutral",
};
export const REFACTOR_LABEL: Record<RefactorStatus, string> = {
  planned: "Planned", blocked: "Blocked", generated: "Diff generated", invalid: "Invalid", validated: "Validated", awaiting_approval: "Awaiting approval", approved: "Approved (not applied)",
  rejected: "Rejected", applying: "Applying", applied: "Applied, NOT verified", verification_failed: "Verification failed", verified: "Verified by tests", analyzed: "Analysis only", closed: "Closed",
};
const SUPPORT_TONE: Record<RefactorSupport, Tone> = { applicable: "success", proposal_only: "info", unsupported: "neutral" };
const SUPPORT_LABEL: Record<RefactorSupport, string> = { applicable: "Applicable", proposal_only: "Proposal only", unsupported: "Unsupported" };
const RENAMES = ["python.rename_symbol", "python.rename_local", "python.rename_method", "js.rename_symbol"];
const JS_LANGS = ["javascript", "jsx", "typescript", "tsx"];

export const RefactorStatusBadge = ({ status }: { status: RefactorStatus }) => <Badge tone={REFACTOR_TONE[status]}>{REFACTOR_LABEL[status]}</Badge>;
export const SupportBadge = ({ level }: { level: RefactorSupport }) => <Badge tone={SUPPORT_TONE[level]}>{SUPPORT_LABEL[level]}</Badge>;

function Catalog({ types }: { types: RefactorType[] }) {
  return (
    <ul className="divide-y divide-slate-200" aria-label="Refactoring catalog">
      {types.map((t) => (
        <li key={t.id} className="py-2" data-testid={`type-${t.id}`}>
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{t.title}</span><code className="text-xs text-slate-500">{t.id}</code><SupportBadge level={t.support} />
            {t.languages.length > 0 && <span className="text-xs text-slate-500">{t.languages.join(", ")}</span>}
          </div>
          {t.support === "unsupported" ? <p className="text-sm text-slate-600">{t.reason}</p> : (
            <details className="mt-1 text-sm">
              <summary className="cursor-pointer text-xs text-slate-600">Requirements, validation and limitations</summary>
              <p className="mt-1"><span className="font-semibold">Scope:</span> {t.scope}</p>
              <p><span className="font-semibold">Semantic basis:</span> {t.semantic_requirement}</p>
              {t.preconditions.length > 0 && <p><span className="font-semibold">Preconditions:</span> {t.preconditions.join("; ")}</p>}
              {t.limitations.length > 0 && <ul className="list-disc pl-5">{t.limitations.map((l) => <li key={l}>{l}</li>)}</ul>}
              {t.reason && <p className="text-slate-600">{t.reason}</p>}
            </details>
          )}
        </li>
      ))}
    </ul>
  );
}

export function RefactorPage() {
  const { repositoryId = "" } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [mode, setMode] = useState<"rename" | "extract" | "duplicates" | "simplify" | "cycles">("rename");
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [q, setQ] = useState("");
  const [typeId, setTypeId] = useState("python.rename_symbol");
  const [relPath, setRelPath] = useState("");
  const [name, setName] = useState("");
  const [newName, setNewName] = useState("");
  const [fn, setFn] = useState("");
  const [cls, setCls] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [minLines, setMinLines] = useState("");
  const [threshold, setThreshold] = useState("");
  const [prefix, setPrefix] = useState("");
  const [noTests, setNoTests] = useState(false);
  const repo = useQuery({ queryKey: ["repository", repositoryId], queryFn: () => api.getRepository(repositoryId) });
  const fresh = useQuery({ queryKey: ["freshness", repositoryId], queryFn: () => api.freshness(repositoryId) });
  const catalog = useQuery({ queryKey: ["refactor-types", repositoryId], queryFn: () => api.refactorTypes(repositoryId) });
  const symbols = useQuery({ queryKey: ["refactor-symbols", repositoryId, q], queryFn: () => api.searchSymbols(repositoryId, { q }), enabled: q.trim().length >= 2 });
  const list = useQuery({ queryKey: ["refactors", repositoryId, page, status, typeFilter], queryFn: () => api.refactors(repositoryId, { page, status, type_id: typeFilter }) });
  const done = (r: { id: string }) => { void qc.invalidateQueries({ queryKey: ["refactors"] }); navigate(`/repositories/${repositoryId}/refactors/${r.id}`); };
  const create = useMutation({
    mutationFn: () => {
      if (mode === "rename") return api.requestRename(repositoryId, { type_id: typeId, rel_path: relPath.trim(), name: name.trim(), new_name: newName.trim(), function: fn.trim() || undefined, class_name: cls.trim() || undefined });
      if (mode === "extract") return api.requestExtract(repositoryId, { rel_path: relPath.trim(), start_line: Number(start), end_line: Number(end), new_name: newName.trim() });
      if (mode === "duplicates") return api.requestDuplicates(repositoryId, { min_lines: minLines ? Number(minLines) : undefined, threshold: threshold ? Number(threshold) : undefined, path_prefix: prefix.trim(), exclude_tests: noTests });
      if (mode === "simplify") return api.requestSimplify(repositoryId, relPath.trim());
      return api.requestCycles(repositoryId, prefix.trim());
    },
    onSuccess: done,
  });
  const valid = mode === "rename" ? Boolean(relPath.trim() && name.trim() && newName.trim()) : mode === "extract" ? Boolean(relPath.trim() && Number(start) >= 1 && Number(end) >= Number(start) && newName.trim())
    : mode === "simplify" ? Boolean(relPath.trim()) : true;
  const types = catalog.data?.types ?? [];
  const f = fresh.data;
  const stale = f && f.state !== "fresh";
  const filtered = Boolean(status || typeFilter);
  return (
    <>
      <PageHeader title="Refactoring workbench" subtitle={repo.data ? `Plan, review, apply and verify code transformations in ${repo.data.name}. Nothing changes until you approve and apply.` : undefined}
        actions={<ButtonLink to={`/repositories/${repositoryId}`} variant="secondary">Back to repository</ButtonLink>} />
      {f && stale && (
        <Notice tone="warning"><span data-testid="stale-index">
          {f.state === "never_indexed" ? "This repository has not been indexed. Index it first." : f.state === "running" ? "An indexing run is in progress; plans may be incomplete until it finishes."
            : f.state === "stale" ? `The index is stale (${f.changed} changed, ${f.new} new, ${f.removed ?? "?"} removed file(s)). Plans read current files, but dependents and tests from the index may be incomplete; new or removed files block transformations.`
            : "Index freshness could not be determined."}
        </span></Notice>
      )}
      <Card title="Catalog">
        {catalog.isLoading ? <Spinner label="Loading catalog…" /> : catalog.isError ? <ErrorState error={catalog.error} onRetry={() => void catalog.refetch()} /> : (
          <>
            <p className="mb-2 text-sm text-slate-600">{Object.entries(catalog.data!.support_levels).map(([k, v]) => `${SUPPORT_LABEL[k as RefactorSupport]}: ${v}`).join(" ")}</p>
            <Catalog types={types} />
          </>
        )}
      </Card>
      <Card title="New request">
        <div role="tablist" aria-label="Request type" className="mb-3 flex flex-wrap gap-2">
          {(["rename", "extract", "duplicates", "simplify", "cycles"] as const).map((m) => (
            <button key={m} role="tab" aria-selected={mode === m} onClick={() => setMode(m)} className={`rounded border px-3 py-1 text-sm ${mode === m ? "border-slate-900 bg-slate-900 text-white" : "border-slate-300"}`}>
              {{ rename: "Rename", extract: "Extract function", duplicates: "Find duplicates", simplify: "Simplify conditionals", cycles: "Import cycles" }[m]}
            </button>
          ))}
        </div>
        <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); if (valid) create.mutate(); }}>
          {mode === "rename" && (
            <>
              <Field label="Find a symbol" htmlFor="rf-q" hint="Search the index, then pick the definition to rename.">
                <TextInput id="rf-q" value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. add_item" />
              </Field>
              {q.trim().length >= 2 && (symbols.isLoading ? <Spinner label="Searching symbols…" /> : symbols.isError ? <ErrorState error={symbols.error} /> : symbols.data!.items.length === 0 ? (
                <EmptyState title="No matching symbols">Check the spelling or re-index.</EmptyState>
              ) : (
                <ul aria-label="Symbol results" className="max-h-48 divide-y divide-slate-100 overflow-auto rounded border border-slate-200 text-sm">
                  {symbols.data!.items.map((s) => (
                    <li key={s.id}>
                      <button type="button" className="flex w-full items-center gap-2 px-2 py-1 text-left hover:bg-slate-50" onClick={() => {
                        setRelPath(s.file_path); setName(s.name);
                        setTypeId(JS_LANGS.includes(s.language.toLowerCase()) ? "js.rename_symbol" : s.kind === "method" ? "python.rename_method" : "python.rename_symbol");
                        if (s.kind === "method") setCls(s.qualified_name.split(".").slice(-2, -1)[0] ?? "");
                      }}>
                        <code>{s.qualified_name}</code><Badge>{s.kind}</Badge><span className="text-xs text-slate-500">{s.file_path}:{s.start_line}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              ))}
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Rename type" htmlFor="rf-type">
                  <select id="rf-type" value={typeId} onChange={(e) => setTypeId(e.target.value)} className="rounded border border-slate-300 px-2 py-1.5 text-sm">
                    {RENAMES.map((t) => <option key={t} value={t}>{types.find((x) => x.id === t)?.title ?? t}{types.find((x) => x.id === t)?.support === "proposal_only" ? " (proposal only)" : ""}</option>)}
                  </select>
                </Field>
                <Field label="File" htmlFor="rf-path"><TextInput id="rf-path" value={relPath} onChange={(e) => setRelPath(e.target.value)} /></Field>
                <Field label="Current name" htmlFor="rf-name"><TextInput id="rf-name" value={name} onChange={(e) => setName(e.target.value)} /></Field>
                <Field label="New name" htmlFor="rf-new"><TextInput id="rf-new" value={newName} onChange={(e) => setNewName(e.target.value)} /></Field>
                {typeId === "python.rename_local" && <Field label="Enclosing function" htmlFor="rf-fn"><TextInput id="rf-fn" value={fn} onChange={(e) => setFn(e.target.value)} /></Field>}
                {typeId === "python.rename_method" && <Field label="Class" htmlFor="rf-cls"><TextInput id="rf-cls" value={cls} onChange={(e) => setCls(e.target.value)} /></Field>}
              </div>
            </>
          )}
          {mode === "extract" && (
            <div className="grid gap-3 sm:grid-cols-4">
              <Field label="File" htmlFor="rf-path"><TextInput id="rf-path" value={relPath} onChange={(e) => setRelPath(e.target.value)} /></Field>
              <Field label="First line" htmlFor="rf-start"><TextInput id="rf-start" inputMode="numeric" value={start} onChange={(e) => setStart(e.target.value)} /></Field>
              <Field label="Last line" htmlFor="rf-end"><TextInput id="rf-end" inputMode="numeric" value={end} onChange={(e) => setEnd(e.target.value)} /></Field>
              <Field label="New function name" htmlFor="rf-new"><TextInput id="rf-new" value={newName} onChange={(e) => setNewName(e.target.value)} /></Field>
            </div>
          )}
          {mode === "duplicates" && (
            <div className="grid gap-3 sm:grid-cols-4">
              <Field label="Minimum lines" htmlFor="rf-ml"><TextInput id="rf-ml" inputMode="numeric" value={minLines} placeholder="default" onChange={(e) => setMinLines(e.target.value)} /></Field>
              <Field label="Similarity threshold (0.5–1)" htmlFor="rf-th"><TextInput id="rf-th" inputMode="decimal" value={threshold} placeholder="default" onChange={(e) => setThreshold(e.target.value)} /></Field>
              <Field label="Only under path" htmlFor="rf-prefix"><TextInput id="rf-prefix" value={prefix} onChange={(e) => setPrefix(e.target.value)} /></Field>
              <label className="flex items-center gap-2 self-end text-sm"><input type="checkbox" checked={noTests} onChange={(e) => setNoTests(e.target.checked)} /> Exclude test files</label>
            </div>
          )}
          {mode === "simplify" && <Field label="File" htmlFor="rf-path"><TextInput id="rf-path" value={relPath} onChange={(e) => setRelPath(e.target.value)} /></Field>}
          {mode === "cycles" && <Field label="Only under path (optional)" htmlFor="rf-prefix"><TextInput id="rf-prefix" value={prefix} onChange={(e) => setPrefix(e.target.value)} /></Field>}
          <Button onClick={() => valid && create.mutate()} disabled={create.isPending || !valid}>{create.isPending ? "Planning…" : mode === "duplicates" || mode === "simplify" || mode === "cycles" ? "Run analysis" : "Create plan"}</Button>
          <p className="text-xs text-slate-500">Planning reads the repository and never changes it.</p>
          {create.error && <MutationError error={create.error} />}
        </form>
      </Card>
      <Card title="History">
        <div className="mb-3 grid gap-2 sm:grid-cols-2">
          <select aria-label="Filter by status" value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }} className="rounded border border-slate-300 px-2 py-1.5 text-sm">
            <option value="">Any status</option>{(Object.keys(REFACTOR_LABEL) as RefactorStatus[]).map((s) => <option key={s} value={s}>{REFACTOR_LABEL[s]}</option>)}
          </select>
          <select aria-label="Filter by type" value={typeFilter} onChange={(e) => { setTypeFilter(e.target.value); setPage(1); }} className="rounded border border-slate-300 px-2 py-1.5 text-sm">
            <option value="">Any type</option>{types.filter((t) => t.support !== "unsupported").map((t) => <option key={t.id} value={t.id}>{t.title}</option>)}
          </select>
        </div>
        {list.isLoading ? <Spinner label="Loading history…" /> : list.isError ? <ErrorState error={list.error} onRetry={() => void list.refetch()} /> : list.data!.items.length === 0 ? (
          <EmptyState title={filtered ? "No refactorings match these filters" : "No refactorings yet"}>{filtered ? "Clear a filter to see more." : "Create a plan or run an analysis above."}</EmptyState>
        ) : (
          <>
            <ul className="divide-y divide-slate-200" aria-label="Refactorings">
              {list.data!.items.map((t) => (
                <li key={t.id} className="py-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <ButtonLink to={`/repositories/${repositoryId}/refactors/${t.id}`} variant="secondary">{t.title}</ButtonLink>
                    <RefactorStatusBadge status={t.status} /><SupportBadge level={t.support_level} />
                    <span className="text-xs text-slate-500">{t.files.length} file(s) · {t.blockers} blocker(s) · {t.warnings} warning(s) · {formatDate(t.created_at)}</span>
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
