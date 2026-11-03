import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Button, ButtonLink, Card, EmptyState, ErrorState, Field, Notice, PageHeader, Spinner, TextArea, TextInput } from "../components/ui";
import { api, describeError } from "../services/api";
import { isPublished, splitLines } from "../services/format";

export function NewTaskPage() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [params] = useSearchParams();
  const repos = useQuery({ queryKey: ["repositories", "all"], queryFn: () => api.listRepositories(1, 100) });
  const [repositoryId, setRepositoryId] = useState(params.get("repository") ?? "");
  const [title, setTitle] = useState("");
  const [problem, setProblem] = useState("");
  const [objective, setObjective] = useState("");
  const [scope, setScope] = useState("");
  const [criteria, setCriteria] = useState("");

  const create = useMutation({
    mutationFn: api.createTask,
    onSuccess: async (task) => {
      await qc.invalidateQueries({ queryKey: ["tasks"] });
      await qc.invalidateQueries({ queryKey: ["overview"] });
      void navigate(`/tasks/${task.id}`);
    },
  });

  if (repos.isPending) return <Spinner />;
  if (repos.isError) return <ErrorState error={repos.error} onRetry={() => void repos.refetch()} />;
  if (repos.data.items.length === 0) {
    return (
      <>
        <PageHeader title="New task" />
        <EmptyState title="Register a repository first" action={<ButtonLink to="/repositories/new">Register repository</ButtonLink>}>
          Tasks are planned against a repository inventory.
        </EmptyState>
      </>
    );
  }
  const selected = repos.data.items.find((r) => r.id === (repositoryId || repos.data.items[0]?.id));
  const effectiveId = selected?.id ?? "";

  const submit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate({
      repository_id: effectiveId,
      title: title.trim(),
      problem_statement: problem.trim(),
      objective: objective.trim(),
      scope: splitLines(scope),
      acceptance_criteria: splitLines(criteria),
    });
  };

  return (
    <>
      <PageHeader title="New engineering task" subtitle="A deterministic baseline planner drafts a plan from your description and the file inventory. No AI model is called, nothing is executed." />
      <form onSubmit={submit} aria-label="New task">
        <Card>
          <div className="space-y-5">
            <Field label="Repository" htmlFor="task-repo">
              <select id="task-repo" value={effectiveId} onChange={(e) => setRepositoryId(e.target.value)}
                className="block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm">
                {repos.data.items.map((r) => (<option key={r.id} value={r.id}>{r.name}</option>))}
              </select>
              {selected && !isPublished(selected.latest_snapshot_status) && (
                <div className="mt-2"><Notice tone="warning">This repository has no completed index, so the plan cannot match files. Index it first for a useful plan.</Notice></div>
              )}
            </Field>
            <Field label="Title" htmlFor="task-title"><TextInput id="task-title" required minLength={3} maxLength={200} value={title} onChange={(e) => setTitle(e.target.value)} /></Field>
            <Field label="Problem statement" htmlFor="task-problem" hint="What is wrong or missing? Mention file paths or module names to improve matching.">
              <TextArea id="task-problem" required minLength={10} rows={4} value={problem} onChange={(e) => setProblem(e.target.value)} />
            </Field>
            <Field label="Objective" htmlFor="task-objective"><TextArea id="task-objective" required minLength={5} rows={2} value={objective} onChange={(e) => setObjective(e.target.value)} /></Field>
            <Field label="Explicit scope (one item per line)" htmlFor="task-scope" hint="What is in and out of scope.">
              <TextArea id="task-scope" rows={3} value={scope} onChange={(e) => setScope(e.target.value)} />
            </Field>
            <Field label="Acceptance criteria (one per line)" htmlFor="task-criteria" hint="At least one. Each should be verifiable.">
              <TextArea id="task-criteria" required rows={3} value={criteria} onChange={(e) => setCriteria(e.target.value)} />
            </Field>
            {create.isError && <Notice tone="danger">{describeError(create.error)}</Notice>}
            <div className="flex gap-2">
              <Button type="submit" disabled={create.isPending || !effectiveId}>{create.isPending ? "Creating…" : "Create task and draft plan"}</Button>
              <ButtonLink to="/tasks" variant="secondary">Cancel</ButtonLink>
            </div>
          </div>
        </Card>
      </form>
    </>
  );
}
