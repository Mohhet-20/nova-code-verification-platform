import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { IndexControls } from "../components/IndexControls";
import { Badge, ButtonLink, Button, EmptyState, ErrorState, Notice, PageHeader, Pagination, Spinner, Stat, TextInput } from "../components/ui";
import { api } from "../services/api";
import type { CyclesPage, DependenciesOut, GraphItem, ImpactOut, Metrics, SymbolDetail, SymbolOut } from "../types/api";

type Tab = "symbols" | "dependencies" | "impact" | "cycles";
type Target = { kind: "file"; path: string } | { kind: "symbol"; id: string; name: string };

const TABS: { id: Tab; label: string }[] = [
  { id: "symbols", label: "Symbols" },
  { id: "dependencies", label: "Dependencies" },
  { id: "impact", label: "Impact" },
  { id: "cycles", label: "Cycles" },
];
const KINDS = ["class", "function", "method", "constructor", "interface", "type_alias", "enum", "constant", "variable", "field", "property"];
const SEVERITY_TONE = { info: "neutral", medium: "warning", high: "danger" } as const;

function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function IntelligencePage() {
  const { repositoryId = "" } = useParams();
  const [tab, setTab] = useState<Tab>("symbols");
  const [target, setTarget] = useState<Target | null>(null);
  const [depPath, setDepPath] = useState("");
  const repo = useQuery({ queryKey: ["repository", repositoryId], queryFn: () => api.getRepository(repositoryId) });
  const metrics = useQuery({ queryKey: ["metrics", repositoryId], queryFn: () => api.metrics(repositoryId) });

  const analyseImpact = (t: Target) => {
    setTarget(t);
    setTab("impact");
  };
  const exploreDeps = (path: string) => {
    setDepPath(path);
    setTab("dependencies");
  };

  return (
    <>
      <PageHeader
        title="Code intelligence"
        subtitle={repo.data ? `Dependency graph, impact and symbols for ${repo.data.name}` : undefined}
        actions={<ButtonLink to={`/repositories/${repositoryId}`} variant="secondary">Back to repository</ButtonLink>}
      />
      <div className="space-y-6">
        <IndexControls repositoryId={repositoryId} />
        <Overview query={metrics} />
        <div>
          <div role="tablist" aria-label="Intelligence views" className="mb-4 flex gap-1 border-b border-slate-200">
            {TABS.map((t) => (
              <button key={t.id} type="button" role="tab" id={`tab-${t.id}`} aria-selected={tab === t.id}
                aria-controls={`panel-${t.id}`} onClick={() => setTab(t.id)}
                className={`-mb-px border-b-2 px-4 py-2 text-sm font-medium ${tab === t.id ? "border-sky-700 text-sky-800" : "border-transparent text-slate-600 hover:text-slate-900"}`}>
                {t.label}
              </button>
            ))}
          </div>
          <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
            {metrics.data && !metrics.data.indexed ? (
              <EmptyState title="Nothing to explore yet">Use “Update index” above to analyse this repository.</EmptyState>
            ) : (
              <>
                {tab === "symbols" && <SymbolExplorer repositoryId={repositoryId} onImpact={analyseImpact} />}
                {tab === "dependencies" && (
                  <DependencyExplorer repositoryId={repositoryId} path={depPath} onPath={setDepPath} onImpact={analyseImpact} />
                )}
                {tab === "impact" && (
                  <ImpactPanel repositoryId={repositoryId} target={target} onTarget={setTarget} onExplore={exploreDeps} />
                )}
                {tab === "cycles" && <Cycles repositoryId={repositoryId} />}
              </>
            )}
          </div>
        </div>
      </div>
    </>
  );
}

// ------------------------------------------------------------------ overview

function Overview({ query }: { query: { isPending: boolean; isError: boolean; error: unknown; data?: Metrics; refetch: () => unknown } }) {
  if (query.isPending) return <Spinner label="Loading metrics…" />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  const m = query.data as Metrics;
  if (!m.indexed) return <Notice tone="info">This repository has no published index yet, so there are no metrics.</Notice>;
  const edges = Object.values(m.edges_by_type).reduce((a, b) => a + b, 0);
  const d = m.definitions;
  const unresolved = (m.imports_by_status.unresolved ?? 0) + (m.imports_by_status.unsupported ?? 0);
  return (
    <section aria-label="Index overview">
      <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Files" value={m.files.toLocaleString()} hint={d.files} />
        <Stat label="Symbols" value={m.symbols.toLocaleString()} hint={d.symbols} />
        <Stat label="Edges" value={edges.toLocaleString()}
          hint={Object.entries(m.edges_by_type).map(([k, v]) => `${k} ${v}`).join(" · ") || "none"} />
        <Stat label="Dependency cycles" value={m.cycles.toLocaleString()} hint="Structural import cycles" />
        <Stat label="Max dependency depth" value={m.max_dependency_depth} hint={d.max_dependency_depth} />
        <Stat label="Unresolved / dynamic imports" value={unresolved.toLocaleString()} hint="Not counted as dependencies" />
        <Stat label="Parse failures" value={m.parse_failures.toLocaleString()} hint={d.parse_failures} />
        <Stat label="Generation" value={m.index?.generation ?? "—"} hint={m.index?.status.replace(/_/g, " ")} />
      </dl>
    </section>
  );
}

// ------------------------------------------------------------------ symbols

function SymbolExplorer({ repositoryId, onImpact }: { repositoryId: string; onImpact: (t: Target) => void }) {
  const [text, setText] = useState("");
  const [kind, setKind] = useState("");
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<string | null>(null);
  const q = useDebounced(text.trim());
  const list = useQuery({
    queryKey: ["intel-symbols", repositoryId, q, kind, page],
    queryFn: ({ signal }) => api.listSymbols(repositoryId, { q, kind, page }, signal),
    placeholderData: (prev) => prev,
  });
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <section aria-labelledby="sym-heading">
        <h2 id="sym-heading" className="sr-only">Symbol explorer</h2>
        <div className="mb-3 flex flex-wrap gap-3">
          <div className="min-w-48 flex-1">
            <label htmlFor="sym-filter" className="sr-only">Filter symbols</label>
            <TextInput id="sym-filter" type="search" placeholder="Filter by name…" value={text}
              onChange={(e) => { setText(e.target.value); setPage(1); }} />
          </div>
          <div>
            <label htmlFor="sym-kind" className="sr-only">Symbol kind</label>
            <select id="sym-kind" value={kind} onChange={(e) => { setKind(e.target.value); setPage(1); }}
              className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm">
              <option value="">All kinds</option>
              {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
          </div>
        </div>
        {list.isPending && <Spinner label="Loading symbols…" />}
        {list.isError && <ErrorState error={list.error} onRetry={() => void list.refetch()} />}
        {list.isSuccess && (list.data.total === 0 ? (
          <EmptyState title="No symbols">No symbols match. Only Python, JavaScript and TypeScript files are parsed.</EmptyState>
        ) : (
          <>
            <ul className="divide-y divide-slate-100 rounded-lg border border-slate-200 bg-white shadow-sm">
              {list.data.items.map((s: SymbolOut) => (
                <li key={s.stable_id}>
                  <button type="button" onClick={() => setSelected(s.stable_id)} aria-pressed={selected === s.stable_id}
                    className={`flex w-full flex-wrap items-baseline justify-between gap-2 px-4 py-2 text-left text-sm hover:bg-slate-50 ${selected === s.stable_id ? "bg-sky-50" : ""}`}>
                    <span className="min-w-0"><span className="font-medium">{s.name}</span> <Badge>{s.kind}</Badge></span>
                    <span className="font-mono text-xs text-slate-500">{s.file_path}:{s.start_line}</span>
                  </button>
                </li>
              ))}
            </ul>
            <Pagination page={list.data.page} pages={list.data.pages} total={list.data.total} onPage={setPage} />
          </>
        ))}
      </section>
      <section aria-label="Symbol detail">
        {selected ? <SymbolPanel repositoryId={repositoryId} symbolId={selected} onImpact={onImpact} />
          : <EmptyState title="Select a symbol">Pick a symbol to see its callers, callees and inheritance.</EmptyState>}
      </section>
    </div>
  );
}

function LinkList({ title, items, total }: { title: string; items: SymbolDetail["callers"]; total?: number }) {
  return (
    <div>
      <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">{title}{total !== undefined ? ` (${total})` : ""}</h4>
      {items.length === 0 ? <p className="text-sm text-slate-500">None found.</p> : (
        <ul className="mt-1 space-y-1 text-sm">
          {items.map((l, i) => (
            <li key={`${l.stable_id ?? l.name}-${i}`}>
              <span className="font-medium">{l.qualified_name ?? l.name}</span>{" "}
              {l.status !== "resolved" && <Badge tone="warning">{l.status}</Badge>}{" "}
              {l.path && <span className="font-mono text-xs text-slate-500">{l.path}{l.line ? `:${l.line}` : ""}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function SymbolPanel({ repositoryId, symbolId, onImpact }: { repositoryId: string; symbolId: string; onImpact: (t: Target) => void }) {
  const d = useQuery({ queryKey: ["intel-symbol", repositoryId, symbolId], queryFn: () => api.symbolDetail(repositoryId, symbolId) });
  if (d.isPending) return <Spinner label="Loading symbol…" />;
  if (d.isError) return <ErrorState error={d.error} onRetry={() => void d.refetch()} />;
  const s = d.data;
  return (
    <div className="space-y-4 rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="text-base font-semibold">{s.qualified_name}</h3>
          <p className="text-xs text-slate-500">
            <Badge>{s.kind}</Badge>{" "}
            <Link className="font-mono text-sky-700 hover:underline" to={`/repositories/${repositoryId}/files/${s.file_id}`}>
              {s.file_path}:{s.start_line}
            </Link>
          </p>
        </div>
        <Button variant="secondary" onClick={() => onImpact({ kind: "symbol", id: s.stable_id, name: s.qualified_name })}>
          Analyse impact
        </Button>
      </div>
      {s.signature && <p className="font-mono text-xs text-slate-600">{s.signature}</p>}
      <LinkList title="Callers" items={s.callers} total={s.caller_total} />
      <LinkList title="Calls" items={s.callees} total={s.callee_total} />
      <LinkList title="Inherits from" items={s.base_links} />
      <LinkList title="Subclasses" items={s.subclasses} />
      {s.unresolved_call_total > 0 && (
        <p className="text-xs text-slate-500">{s.unresolved_call_total} call site(s) inside this symbol could not be resolved to a single target.</p>
      )}
      <p className="text-xs text-slate-500">{s.note}</p>
    </div>
  );
}

// ------------------------------------------------------------------ dependencies

function ItemList({ items, onImpact }: { items: GraphItem[]; onImpact?: (path: string) => void }) {
  return (
    <ul className="divide-y divide-slate-100 rounded-lg border border-slate-200 bg-white shadow-sm">
      {items.map((i) => (
        <li key={`${i.kind}-${i.id}`} className="px-4 py-2 text-sm">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <span className="min-w-0">
              <span className="font-mono text-xs">{i.path || i.name}</span>{" "}
              <Badge>depth {i.depth}</Badge>{" "}
              <Badge tone={i.status === "resolved" ? "success" : "warning"}>{i.status}</Badge>{" "}
              <span className="text-xs text-slate-500">{i.relationship.replace(/_/g, " ").toLowerCase()} · {i.evidence}{i.line ? ` · line ${i.line}` : ""}</span>
            </span>
            {onImpact && i.kind === "file" && (
              <button type="button" className="text-xs text-sky-700 hover:underline" onClick={() => onImpact(i.path)}>
                Impact of {i.name}
              </button>
            )}
          </div>
          <p className="mt-0.5 text-xs text-slate-600">{i.explanation}</p>
          {i.chain.length > 2 && <p className="mt-0.5 break-all font-mono text-xs text-slate-500">{i.chain.join(" → ")}</p>}
        </li>
      ))}
    </ul>
  );
}

function DependencyExplorer({ repositoryId, path, onPath, onImpact }: {
  repositoryId: string; path: string; onPath: (p: string) => void; onImpact: (t: Target) => void;
}) {
  const [direction, setDirection] = useState<"out" | "in">("out");
  const [depth, setDepth] = useState(2);
  const [page, setPage] = useState(1);
  const [draft, setDraft] = useState(path);
  const [prevPath, setPrevPath] = useState(path);
  if (path !== prevPath) {
    // Another view asked to explore a different file: adopt it (state adjusted during render, not in an effect).
    setPrevPath(path);
    setDraft(path);
    setPage(1);
  }
  const p = path.trim();
  const res = useQuery<DependenciesOut>({
    queryKey: ["intel-deps", repositoryId, p, direction, depth, page],
    queryFn: () => api.dependencies(repositoryId, { path: p, direction, depth, page }),
    enabled: p.length > 0,
    placeholderData: (prev) => prev,
  });
  return (
    <section aria-labelledby="dep-heading">
      <h2 id="dep-heading" className="sr-only">Dependency explorer</h2>
      <form className="mb-3 flex flex-wrap items-end gap-3" onSubmit={(e) => { e.preventDefault(); setPage(1); onPath(draft.trim()); }}>
        <div className="min-w-64 flex-1">
          <label htmlFor="dep-path" className="mb-1 block text-xs font-medium text-slate-600">File path (relative to the repository)</label>
          <TextInput id="dep-path" placeholder="e.g. src/app/main.py" value={draft} onChange={(e) => setDraft(e.target.value)} />
        </div>
        <div>
          <label htmlFor="dep-direction" className="mb-1 block text-xs font-medium text-slate-600">Direction</label>
          <select id="dep-direction" value={direction} onChange={(e) => { setDirection(e.target.value as "out" | "in"); setPage(1); }}
            className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm">
            <option value="out">Depends on</option>
            <option value="in">Depended on by</option>
          </select>
        </div>
        <div>
          <label htmlFor="dep-depth" className="mb-1 block text-xs font-medium text-slate-600">Depth</label>
          <select id="dep-depth" value={depth} onChange={(e) => { setDepth(Number(e.target.value)); setPage(1); }}
            className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm">
            {[1, 2, 3, 5, 10].map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </div>
        <Button type="submit" disabled={!draft.trim()}>Explore</Button>
      </form>
      {p.length === 0 && <p className="text-sm text-slate-500">Enter a file path to see what it depends on or what depends on it.</p>}
      {p.length > 0 && res.isPending && <Spinner label="Loading dependencies…" />}
      {res.isError && <ErrorState error={res.error} onRetry={() => void res.refetch()} />}
      {res.isSuccess && (
        <>
          <p className="mb-2 text-xs text-slate-500">
            Generation {res.data.index.generation} · {res.data.total} file{res.data.total === 1 ? "" : "s"} within {res.data.max_depth} step{res.data.max_depth === 1 ? "" : "s"}
          </p>
          {res.data.truncated && <div className="mb-2"><Notice tone="warning">The traversal hit its node limit; results are incomplete.</Notice></div>}
          {res.data.more_beyond_depth && <div className="mb-2"><Notice tone="info">More files exist beyond this depth. Increase the depth to follow them.</Notice></div>}
          {res.data.items.length === 0 ? (
            <EmptyState title="No resolved file dependencies">
              {direction === "out" ? "This file has no imports that resolve to other repository files." : "No repository file imports this one."}
            </EmptyState>
          ) : (
            <>
              <ItemList items={res.data.items} onImpact={(pth) => onImpact({ kind: "file", path: pth })} />
              <Pagination page={res.data.page} pages={res.data.pages} total={res.data.total} onPage={setPage} />
            </>
          )}
          {direction === "out" && res.data.unresolved_total > 0 && (
            <div className="mt-4">
              <h3 className="text-sm font-semibold">Imports that could not be resolved ({res.data.unresolved_total})</h3>
              <ul className="mt-1 space-y-1 text-sm">
                {res.data.unresolved.map((u, i) => (
                  <li key={`${u.specifier}-${i}`}>
                    <span className="font-mono text-xs">{u.specifier}</span> <Badge tone="warning">{u.status}</Badge>{" "}
                    <span className="text-xs text-slate-500">line {u.line}{u.note ? ` · ${u.note}` : ""}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <p className="mt-3 text-xs text-slate-500">{res.data.note}</p>
        </>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ impact

function ImpactPanel({ repositoryId, target, onTarget, onExplore }: {
  repositoryId: string; target: Target | null; onTarget: (t: Target | null) => void; onExplore: (p: string) => void;
}) {
  const [maxDepth, setMaxDepth] = useState(5);
  const [page, setPage] = useState(1);
  const [draft, setDraft] = useState(target?.kind === "file" ? target.path : "");
  const [prevTarget, setPrevTarget] = useState(target);
  if (target !== prevTarget) {
    setPrevTarget(target);
    setPage(1);
    if (target?.kind === "file") setDraft(target.path);
  }
  const res = useQuery<ImpactOut>({
    queryKey: ["intel-impact", repositoryId, target, maxDepth, page],
    queryFn: () => api.impact(repositoryId, {
      path: target?.kind === "file" ? target.path : undefined,
      symbolId: target?.kind === "symbol" ? target.id : undefined,
      maxDepth, page,
    }),
    enabled: target !== null,
    placeholderData: (prev) => prev,
  });
  return (
    <section aria-labelledby="impact-heading">
      <h2 id="impact-heading" className="sr-only">Change impact</h2>
      <form className="mb-3 flex flex-wrap items-end gap-3" onSubmit={(e) => { e.preventDefault(); if (draft.trim()) onTarget({ kind: "file", path: draft.trim() }); }}>
        <div className="min-w-64 flex-1">
          <label htmlFor="impact-path" className="mb-1 block text-xs font-medium text-slate-600">File to change</label>
          <TextInput id="impact-path" placeholder="e.g. src/app/models.py" value={draft} onChange={(e) => setDraft(e.target.value)} />
        </div>
        <div>
          <label htmlFor="impact-depth" className="mb-1 block text-xs font-medium text-slate-600">Max depth</label>
          <select id="impact-depth" value={maxDepth} onChange={(e) => { setMaxDepth(Number(e.target.value)); setPage(1); }}
            className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm">
            {[1, 2, 3, 5, 10].map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </div>
        <Button type="submit" disabled={!draft.trim()}>Analyse impact</Button>
      </form>
      {target === null && <p className="text-sm text-slate-500">Choose a file here, or pick a symbol or dependency elsewhere and select “Analyse impact”.</p>}
      {target !== null && res.isPending && <Spinner label="Analysing impact…" />}
      {res.isError && <ErrorState error={res.error} onRetry={() => void res.refetch()} />}
      {res.isSuccess && (
        <>
          <div className="mb-3 rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
            <p className="text-sm">
              If <span className="font-mono">{res.data.target_name}</span> ({res.data.target_kind}) changes,{" "}
              <strong>{res.data.summary.total}</strong> item{res.data.summary.total === 1 ? "" : "s"} may be affected:{" "}
              {res.data.summary.direct} directly, {res.data.summary.transitive} transitively, across{" "}
              {res.data.summary.files_affected} file{res.data.summary.files_affected === 1 ? "" : "s"}.
            </p>
            {res.data.target_kind === "file" && (
              <button type="button" className="mt-1 text-xs text-sky-700 hover:underline" onClick={() => onExplore(res.data.target_path)}>
                Open in dependency explorer
              </button>
            )}
          </div>
          <div className="mb-3"><Notice tone="info">{res.data.disclaimer}</Notice></div>
          {res.data.cycles.length > 0 && (
            <div className="mb-3">
              <Notice tone="warning">
                Part of this graph is in a structural dependency cycle ({res.data.cycles.map((c) => `${c.size} file${c.size === 1 ? "" : "s"}, ${c.severity}`).join("; ")}).
                Each file is reported once at its shortest distance.
              </Notice>
            </div>
          )}
          {res.data.truncated && <div className="mb-3"><Notice tone="warning">The traversal hit its node limit; the list is incomplete.</Notice></div>}
          {res.data.more_beyond_depth && <div className="mb-3"><Notice tone="info">More dependents exist beyond depth {res.data.max_depth}.</Notice></div>}
          {res.data.items.length === 0 ? (
            <EmptyState title="Nothing depends on this">No resolved importers, callers or subclasses were found.</EmptyState>
          ) : (
            <>
              <ItemList items={res.data.items} />
              <Pagination page={res.data.page} pages={res.data.pages} total={res.data.total} onPage={setPage} />
            </>
          )}
        </>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ cycles

function Cycles({ repositoryId }: { repositoryId: string }) {
  const [page, setPage] = useState(1);
  const res = useQuery<CyclesPage>({
    queryKey: ["intel-cycles", repositoryId, page],
    queryFn: () => api.cycles(repositoryId, page),
    placeholderData: (prev) => prev,
  });
  if (res.isPending) return <Spinner label="Looking for cycles…" />;
  if (res.isError) return <ErrorState error={res.error} onRetry={() => void res.refetch()} />;
  const d = res.data;
  return (
    <section aria-labelledby="cycles-heading">
      <h2 id="cycles-heading" className="sr-only">Dependency cycles</h2>
      <div className="mb-3"><Notice tone="info">{d.note}</Notice></div>
      {d.items.length === 0 ? (
        <EmptyState title="No dependency cycles">No group of files imports each other in the resolved import graph.</EmptyState>
      ) : (
        <>
          <ul className="space-y-3">
            {d.items.map((c) => (
              <li key={c.id} className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
                <p className="text-sm font-medium">
                  <Badge tone={SEVERITY_TONE[c.severity]}>{c.severity}</Badge>{" "}
                  {c.size === 1 ? "Self-import" : `${c.size}-file cycle`}
                </p>
                <p className="mt-1 text-xs text-slate-600">{c.severity_reason}. {c.explanation}</p>
                <ul className="mt-2 font-mono text-xs text-slate-700">
                  {c.files.map((f) => <li key={f.file_id}>{f.path}</li>)}
                </ul>
                {c.files_truncated && <p className="text-xs text-slate-500">Showing the first {c.files.length} files.</p>}
              </li>
            ))}
          </ul>
          <Pagination page={d.page} pages={d.pages} total={d.total} onPage={setPage} />
        </>
      )}
    </section>
  );
}
