import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Badge, ButtonLink, EmptyState, ErrorState, PageHeader, Pagination, Spinner, statusTone } from "../components/ui";
import { api } from "../services/api";
import { formatDate } from "../services/format";

export function TasksPage() {
  const [page, setPage] = useState(1);
  const tasks = useQuery({ queryKey: ["tasks", page], queryFn: () => api.listTasks(page, 20) });
  return (
    <>
      <PageHeader
        title="Engineering tasks"
        subtitle="Each task carries an inspectable draft plan. Plans never modify files or run commands."
        actions={<ButtonLink to="/tasks/new">New task</ButtonLink>}
      />
      {tasks.isPending && <Spinner />}
      {tasks.isError && <ErrorState error={tasks.error} onRetry={() => void tasks.refetch()} />}
      {tasks.isSuccess && tasks.data.items.length === 0 && (
        <EmptyState title="No tasks yet" action={<ButtonLink to="/tasks/new">Create a task</ButtonLink>}>
          Describe a change you want to make and NOVA will draft a plan from the repository inventory.
        </EmptyState>
      )}
      {tasks.isSuccess && tasks.data.items.length > 0 && (
        <>
          <ul className="divide-y divide-slate-100 rounded-lg border border-slate-200 bg-white shadow-sm">
            {tasks.data.items.map((t) => (
              <li key={t.id} className="flex flex-wrap items-center justify-between gap-3 px-5 py-4">
                <div className="min-w-0">
                  <Link to={`/tasks/${t.id}`} className="font-medium text-sky-700 hover:underline">{t.title}</Link>
                  <p className="mt-0.5 text-xs text-slate-500">{t.repository_name} · created {formatDate(t.created_at)}</p>
                </div>
                <Badge tone={statusTone(t.status)}>{t.status}</Badge>
              </li>
            ))}
          </ul>
          <Pagination page={tasks.data.page} pages={tasks.data.pages} total={tasks.data.total} onPage={setPage} />
        </>
      )}
    </>
  );
}
