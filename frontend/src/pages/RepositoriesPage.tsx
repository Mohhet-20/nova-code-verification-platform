import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Badge, ButtonLink, EmptyState, ErrorState, PageHeader, Pagination, Spinner, statusTone } from "../components/ui";
import { api } from "../services/api";
import { formatDate } from "../services/format";

export function RepositoriesPage() {
  const [page, setPage] = useState(1);
  const repos = useQuery({ queryKey: ["repositories", page], queryFn: () => api.listRepositories(page, 20) });

  return (
    <>
      <PageHeader
        title="Repositories"
        subtitle="Local repositories registered with NOVA. Only the folder name is shown here."
        actions={<ButtonLink to="/repositories/new">Register repository</ButtonLink>}
      />
      {repos.isPending && <Spinner />}
      {repos.isError && <ErrorState error={repos.error} onRetry={() => void repos.refetch()} />}
      {repos.isSuccess && repos.data.items.length === 0 && (
        <EmptyState title="No repositories registered" action={<ButtonLink to="/repositories/new">Register repository</ButtonLink>}>
          Registration points NOVA at a directory on the machine running the backend.
        </EmptyState>
      )}
      {repos.isSuccess && repos.data.items.length > 0 && (
        <>
          <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white shadow-sm">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Registered repositories</caption>
              <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th scope="col" className="px-4 py-3">Name</th>
                  <th scope="col" className="px-4 py-3">Folder</th>
                  <th scope="col" className="px-4 py-3">Git</th>
                  <th scope="col" className="px-4 py-3">Index</th>
                  <th scope="col" className="px-4 py-3 text-right">Files</th>
                  <th scope="col" className="px-4 py-3">Registered</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {repos.data.items.map((r) => (
                  <tr key={r.id} className="hover:bg-slate-50">
                    <td className="px-4 py-3 font-medium">
                      <Link className="text-sky-700 hover:underline" to={`/repositories/${r.id}`}>{r.name}</Link>
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-slate-600">{r.root_label}</td>
                    <td className="px-4 py-3">{r.is_git ? <Badge tone="info">{r.git_branch ?? "git"}</Badge> : <Badge>not git</Badge>}</td>
                    <td className="px-4 py-3">
                      <Badge tone={statusTone(r.latest_snapshot_status)}>{r.latest_snapshot_status ?? "not indexed"}</Badge>
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums">{r.files_indexed?.toLocaleString() ?? "—"}</td>
                    <td className="px-4 py-3 text-slate-600">{formatDate(r.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination page={repos.data.page} pages={repos.data.pages} total={repos.data.total} onPage={setPage} />
        </>
      )}
    </>
  );
}
