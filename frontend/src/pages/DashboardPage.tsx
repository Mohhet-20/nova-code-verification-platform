import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ButtonLink, Card, EmptyState, ErrorState, PageHeader, Spinner, Stat } from "../components/ui";
import { api } from "../services/api";
import { formatDate } from "../services/format";

const ACTION_LABELS: Record<string, string> = {
  "repository.registered": "Repository registered",
  "index.started": "Indexing started",
  "index.completed": "Indexing completed",
  "index.failed": "Indexing failed",
  "task.created": "Task created with draft plan",
  "plan.edited": "Plan edited",
  "plan.regenerated": "Plan regenerated",
};

export function DashboardPage() {
  const overview = useQuery({ queryKey: ["overview"], queryFn: api.overview });

  if (overview.isPending) return <Spinner />;
  if (overview.isError) return <ErrorState error={overview.error} onRetry={() => void overview.refetch()} />;
  const o = overview.data;

  return (
    <>
      <PageHeader
        title="Overview"
        subtitle="Live counts from your local NOVA workspace. Nothing here is estimated or sample data."
        actions={<ButtonLink to="/repositories/new">Register repository</ButtonLink>}
      />
      {o.repositories === 0 ? (
        <EmptyState
          title="No repositories yet"
          action={<ButtonLink to="/repositories/new">Register your first repository</ButtonLink>}
        >
          Register a local repository, index it, then create an engineering task to get a draft plan.
        </EmptyState>
      ) : (
        <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Stat label="Repositories" value={o.repositories} />
          <Stat label="Indexed" value={o.indexed_repositories} hint="with a completed snapshot" />
          <Stat label="Indexed files" value={o.indexed_files.toLocaleString()} />
          <Stat
            label="Tasks"
            value={o.tasks_total}
            hint={Object.entries(o.tasks_by_status).map(([s, c]) => `${c} ${s}`).join(" · ") || undefined}
          />
        </dl>
      )}
      <div className="mt-8 grid gap-6 lg:grid-cols-3">
        <Card title="Recent activity" className="lg:col-span-2">
          {o.recent_events.length === 0 ? (
            <p className="text-sm text-slate-600">No activity recorded yet.</p>
          ) : (
            <ul className="divide-y divide-slate-100">
              {o.recent_events.map((e) => (
                <li key={e.id} className="flex items-center justify-between gap-4 py-2.5 text-sm">
                  <span className="text-slate-800">{ACTION_LABELS[e.action] ?? e.action}</span>
                  <time className="shrink-0 text-xs text-slate-500" dateTime={e.created_at}>
                    {formatDate(e.created_at)}
                  </time>
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card title="Where to go next">
          <ul className="space-y-2 text-sm">
            <li><Link className="text-sky-700 hover:underline" to="/repositories">Browse repositories</Link></li>
            <li><Link className="text-sky-700 hover:underline" to="/tasks/new">Create an engineering task</Link></li>
            <li><Link className="text-sky-700 hover:underline" to="/settings">Security model &amp; help</Link></li>
          </ul>
        </Card>
      </div>
    </>
  );
}
