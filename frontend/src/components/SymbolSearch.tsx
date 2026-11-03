import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../services/api";
import { MATCH_LABELS } from "../services/format";
import { Badge, EmptyState, ErrorState, Notice, Pagination, Spinner, TextInput } from "./ui";

const KINDS = ["class", "function", "method", "constructor", "interface", "type_alias", "enum", "constant", "variable", "field", "property"];

function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

/** Search symbols in the newest published index. Every hit says why it matched and where it is. */
export function SymbolSearch({ repositoryId, indexed }: { repositoryId: string; indexed: boolean }) {
  const [text, setText] = useState("");
  const [kind, setKind] = useState("");
  const [page, setPage] = useState(1);
  const q = useDebounced(text.trim());
  const result = useQuery({
    queryKey: ["symbol-search", repositoryId, q, kind, page],
    queryFn: () => api.searchSymbols(repositoryId, { q, kind, page }),
    enabled: indexed && q.length > 0,
    placeholderData: (prev) => prev,
  });

  return (
    <section aria-labelledby="symbols-heading" className="mt-8">
      <h2 id="symbols-heading" className="mb-1 text-lg font-semibold">Symbol search</h2>
      <p className="mb-3 text-sm text-slate-600">
        Structural search over parsed files (names and locations only; source is never shown). Results come from the
        last published index, not from the files on disk right now.
      </p>
      {!indexed ? (
        <EmptyState title="Nothing to search yet">Index the repository to extract symbols.</EmptyState>
      ) : (
        <>
          <div className="mb-3 flex flex-wrap gap-3">
            <div className="min-w-64 flex-1">
              <label htmlFor="symbol-search" className="sr-only">Search symbols</label>
              <TextInput id="symbol-search" type="search" placeholder="Search symbols, e.g. UserService…" value={text}
                onChange={(e) => { setText(e.target.value); setPage(1); }} />
            </div>
            <div>
              <label htmlFor="symbol-kind" className="sr-only">Filter by kind</label>
              <select id="symbol-kind" value={kind} onChange={(e) => { setKind(e.target.value); setPage(1); }}
                className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm">
                <option value="">All kinds</option>
                {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
              </select>
            </div>
          </div>
          {q.length === 0 && <p className="text-sm text-slate-500">Type to search.</p>}
          {q.length > 0 && result.isPending && <Spinner label="Searching…" />}
          {result.isError && <ErrorState error={result.error} onRetry={() => void result.refetch()} />}
          {result.isSuccess && (
            <>
              {result.data.index && (
                <p className="mb-2 text-xs text-slate-500">
                  Index generation {result.data.index.generation} ({result.data.index.status.replace(/_/g, " ")})
                </p>
              )}
              {result.data.ambiguous && (
                <div className="mb-2"><Notice tone="info">Several different symbols have exactly this name. All candidates are listed; none was guessed.</Notice></div>
              )}
              {result.data.total === 0 ? (
                <EmptyState title="No symbols match">Only parsed languages (Python, JavaScript, TypeScript) have symbols.</EmptyState>
              ) : (
                <>
                  <ul className="divide-y divide-slate-100 rounded-lg border border-slate-200 bg-white shadow-sm">
                    {result.data.items.map((s) => (
                      <li key={s.id} className="flex flex-wrap items-baseline justify-between gap-2 px-4 py-2 text-sm">
                        <span className="min-w-0">
                          <span className="font-medium">{s.name}</span>{" "}
                          <Badge>{s.kind}</Badge>{" "}
                          <span className="text-xs text-slate-500">{MATCH_LABELS[s.matched_on] ?? s.matched_on}</span>
                          {s.qualified_name !== s.name && (
                            <span className="block truncate font-mono text-xs text-slate-500">{s.qualified_name}</span>
                          )}
                        </span>
                        <Link className="font-mono text-xs text-sky-700 hover:underline"
                          to={`/repositories/${repositoryId}/files/${s.file_id}`}>
                          {s.file_path}:{s.start_line}
                        </Link>
                      </li>
                    ))}
                  </ul>
                  <Pagination page={result.data.page} pages={result.data.pages} total={result.data.total} onPage={setPage} />
                </>
              )}
            </>
          )}
        </>
      )}
    </section>
  );
}
