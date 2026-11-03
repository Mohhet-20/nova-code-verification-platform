import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, describeError } from "../services/api";
import { formatDate } from "../services/format";
import { Badge, Button, ErrorState, Notice, Spinner, statusTone } from "./ui";

/** Index state, files awaiting indexing, and the only two ways to change the index: update or rebuild. */
export function IndexControls({ repositoryId }: { repositoryId: string }) {
  const qc = useQueryClient();
  const [confirmRebuild, setConfirmRebuild] = useState(false);
  const status = useQuery({
    queryKey: ["index-status", repositoryId],
    queryFn: () => api.indexStatus(repositoryId, true),
    refetchInterval: (q) => (q.state.data?.state === "running" ? 1500 : false),
  });
  const refresh = () => {
    for (const key of ["index-status", "metrics", "intel-symbols", "intel-deps", "intel-impact", "intel-cycles", "summary", "repository", "freshness"]) {
      void qc.invalidateQueries({ queryKey: [key] });
    }
  };
  const start = useMutation({
    mutationFn: (mode: "incremental" | "full") =>
      mode === "full" ? api.rebuildIndex(repositoryId) : api.startIndexing(repositoryId, mode),
    onSuccess: () => {
      setConfirmRebuild(false);
      refresh();
    },
    onError: refresh,
  });

  const wasRunning = status.data?.state === "running";
  const busy = start.isPending || wasRunning;

  if (status.isPending) return <Spinner label="Loading index status…" />;
  if (status.isError) return <ErrorState error={status.error} onRetry={() => void status.refetch()} />;
  const s = status.data;
  const awaiting = s.files_awaiting_indexing;

  return (
    <section aria-labelledby="index-controls-heading" className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 id="index-controls-heading" className="text-sm font-semibold text-slate-800">Index</h2>
          <p className="mt-1 flex flex-wrap items-center gap-2 text-sm text-slate-700">
            <Badge tone={statusTone(s.state === "never_indexed" ? null : s.state)}>{s.state.replace(/_/g, " ")}</Badge>
            {s.serving && (
              <span>
                answering from generation {s.serving.generation}
                {s.serving.finished_at ? `, finished ${formatDate(s.serving.finished_at)}` : ""}
              </span>
            )}
            {awaiting !== null && (
              <span className="text-slate-500">
                · {awaiting === 0 ? "no files awaiting indexing" : `${awaiting.toLocaleString()} file${awaiting === 1 ? "" : "s"} awaiting indexing`}
              </span>
            )}
          </p>
          <p className="mt-1 text-xs text-slate-500">{s.message}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button onClick={() => start.mutate("incremental")} disabled={busy}>
            {busy && !confirmRebuild ? "Indexing…" : "Update index"}
          </Button>
          {confirmRebuild ? (
            <>
              <Button variant="danger" onClick={() => start.mutate("full")} disabled={busy}>Confirm rebuild</Button>
              <Button variant="ghost" onClick={() => setConfirmRebuild(false)} disabled={busy}>Cancel</Button>
            </>
          ) : (
            <Button variant="secondary" onClick={() => setConfirmRebuild(true)} disabled={busy}>Rebuild index</Button>
          )}
        </div>
      </div>
      {wasRunning && (
        <p role="status" className="mt-3 flex items-center gap-2 text-sm text-slate-600">
          <span className="h-3 w-3 animate-spin rounded-full border-2 border-slate-300 border-t-sky-700" />
          Indexing in progress. Results below stay on the previous generation until it finishes.
        </p>
      )}
      {confirmRebuild && !busy && (
        <p className="mt-3 text-xs text-slate-600">
          A rebuild re-parses every file instead of reusing unchanged results. Use it after a parser change or if the
          index looks wrong. The current index keeps answering queries until the new one is complete.
        </p>
      )}
      {start.isError && <div className="mt-3"><Notice tone="danger">{describeError(start.error)}</Notice></div>}
      {s.state === "failed" && (
        <div className="mt-3">
          <Notice tone="danger">
            Last run failed: {s.last_failure ?? "unknown error"}.{" "}
            {s.serving ? "Queries still use the previous generation." : "Nothing is queryable yet."}
          </Notice>
        </div>
      )}
      {s.state === "completed_with_warnings" && (
        <div className="mt-3"><Notice tone="warning">The index is usable but some files could not be fully analysed.</Notice></div>
      )}
      {s.pending?.state === "stale" && (
        <div className="mt-3">
          <Notice tone="info">
            Files changed since the last run (estimate from size and modification time): {s.pending.changed} changed,{" "}
            {s.pending.new} new{s.pending.removed !== null ? `, ${s.pending.removed} removed` : ""}.
          </Notice>
        </div>
      )}
    </section>
  );
}
