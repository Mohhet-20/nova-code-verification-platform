import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Badge, Button, Card, ErrorState, Notice, PageHeader, Spinner, statusTone, TextArea, TextInput } from "../components/ui";
import { api, describeError } from "../services/api";
import { formatDate, splitLines } from "../services/format";
import type { ExpectedCheck, PlanContent, PlanStep, TaskDetail } from "../types/api";

export function TaskDetailPage() {
  const { taskId = "" } = useParams();
  const task = useQuery({ queryKey: ["task", taskId], queryFn: () => api.getTask(taskId) });
  if (task.isPending) return <Spinner />;
  if (task.isError) return <ErrorState error={task.error} onRetry={() => void task.refetch()} />;
  return <TaskView task={task.data} />;
}

function TaskView({ task }: { task: TaskDetail }) {
  const qc = useQueryClient();
  const plan = task.plan;
  const [draft, setDraft] = useState<PlanContent | null>(plan?.content ?? null);
  const [confirmRegen, setConfirmRegen] = useState(false);
  const [saved, setSaved] = useState(false);

  const dirty = JSON.stringify(draft) !== JSON.stringify(plan?.content);

  const refresh = async (t: TaskDetail) => {
    qc.setQueryData(["task", task.id], t);
    await qc.invalidateQueries({ queryKey: ["tasks"] });
  };
  const save = useMutation({
    mutationFn: (c: PlanContent) => api.savePlan(task.id, c),
    onSuccess: async (t) => { setDraft(t.plan?.content ?? null); setSaved(true); await refresh(t); },
  });
  const regen = useMutation({
    mutationFn: () => api.regeneratePlan(task.id),
    onSuccess: async (t) => { setDraft(t.plan?.content ?? null); setConfirmRegen(false); setSaved(false); await refresh(t); },
  });

  const edit = (patch: Partial<PlanContent>) => { setSaved(false); setDraft((d) => (d ? { ...d, ...patch } : d)); };
  const editStep = (i: number, patch: Partial<PlanStep>) =>
    edit({ steps: draft!.steps.map((s, idx) => (idx === i ? { ...s, ...patch } : s)) });
  const editCheck = (i: number, patch: Partial<ExpectedCheck>) =>
    edit({ expected_checks: draft!.expected_checks.map((c, idx) => (idx === i ? { ...c, ...patch } : c)) });

  return (
    <>
      <PageHeader
        title={task.title}
        subtitle={<>
          <Link className="text-sky-700 hover:underline" to={`/repositories/${task.repository_id}`}>{task.repository_name}</Link>
          {" · "}created {formatDate(task.created_at)}
        </>}
        actions={<Badge tone={statusTone(task.status)}>{task.status}</Badge>}
      />
      <div className="grid gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-1">
          <Card title="Task">
            <dl className="space-y-4 text-sm">
              <Block label="Problem">{task.problem_statement}</Block>
              <Block label="Objective">{task.objective}</Block>
              <ListBlock label="Scope" items={task.scope} empty="No explicit scope." />
              <ListBlock label="Acceptance criteria" items={task.acceptance_criteria} />
            </dl>
          </Card>
        </div>
        <div className="space-y-6 lg:col-span-2">
          {!plan || !draft ? (
            <Notice tone="warning">This task has no plan.</Notice>
          ) : (
            <>
              <Notice tone="info">
                <strong>Draft plan — deterministic baseline, not an AI agent.</strong> Generated from task text and file
                names only (revision {plan.revision}{plan.edited_by_user ? ", edited by you" : ""}). Nothing has been
                changed or executed. Steps marked “approval” would require your explicit approval in later work.
              </Notice>
              <Card title="Summary" actions={<Badge tone={draft.confidence === "low" ? "warning" : "success"}>{draft.confidence} confidence</Badge>}>
                <p className="text-sm text-slate-700">{draft.summary}</p>
                {draft.warnings.length > 0 && (
                  <ul className="mt-3 space-y-2">{draft.warnings.map((w) => (<li key={w}><Notice tone="warning">{w}</Notice></li>))}</ul>
                )}
                <details className="mt-3 text-xs text-slate-600">
                  <summary className="cursor-pointer font-medium">Assumptions</summary>
                  <ul className="mt-1 list-disc pl-5">{draft.assumptions.map((a) => (<li key={a}>{a}</li>))}</ul>
                </details>
              </Card>

              <Card title="Relevant files (heuristic)">
                <label htmlFor="plan-files" className="sr-only">Relevant files, one per line</label>
                <TextArea id="plan-files" rows={Math.max(3, draft.relevant_files.length + 1)} className="font-mono text-xs"
                  value={draft.relevant_files.join("\n")} onChange={(e) => edit({ relevant_files: splitLines(e.target.value) })} />
                <p className="mt-1 text-xs text-slate-500">One path per line. Matches are based on file names, not contents.</p>
              </Card>

              <Card title="Steps" actions={<Button variant="secondary" onClick={() => edit({ steps: [...draft.steps, { id: `s${Date.now().toString(36)}`, kind: "implement", title: "New step", description: "", target_paths: [], requires_approval: true }] })}>Add step</Button>}>
                <ol className="space-y-4">
                  {draft.steps.map((s, i) => (
                    <li key={s.id} className="rounded-md border border-slate-200 p-3">
                      <div className="mb-2 flex items-center justify-between gap-2">
                        <span className="flex items-center gap-2 text-xs"><Badge tone="info">{i + 1}. {s.kind}</Badge>{s.requires_approval && <Badge tone="warning">approval</Badge>}</span>
                        <Button variant="ghost" aria-label={`Remove step ${i + 1}`} disabled={draft.steps.length <= 1}
                          onClick={() => edit({ steps: draft.steps.filter((_, idx) => idx !== i) })}>Remove</Button>
                      </div>
                      <label htmlFor={`step-title-${i}`} className="sr-only">Step {i + 1} title</label>
                      <TextInput id={`step-title-${i}`} value={s.title} maxLength={200} onChange={(e) => editStep(i, { title: e.target.value })} />
                      <label htmlFor={`step-desc-${i}`} className="sr-only">Step {i + 1} description</label>
                      <TextArea id={`step-desc-${i}`} className="mt-2" rows={3} maxLength={2000} value={s.description} onChange={(e) => editStep(i, { description: e.target.value })} />
                      {s.target_paths.length > 0 && <p className="mt-2 break-all font-mono text-xs text-slate-500">{s.target_paths.join(" · ")}</p>}
                    </li>
                  ))}
                </ol>
              </Card>

              <Card title="Expected verification checks" actions={<Button variant="secondary" onClick={() => edit({ expected_checks: [...draft.expected_checks, { name: "New check", suggested_command: null, rationale: "", executed: false }] })}>Add check</Button>}>
                <p className="mb-3 text-xs text-slate-500">Suggestions only. NOVA has not run any of these and cannot in this version.</p>
                <ul className="space-y-3">
                  {draft.expected_checks.map((c, i) => (
                    <li key={i} className="rounded-md border border-slate-200 p-3">
                      <div className="flex gap-2">
                        <div className="flex-1">
                          <label htmlFor={`check-name-${i}`} className="sr-only">Check {i + 1} name</label>
                          <TextInput id={`check-name-${i}`} value={c.name} maxLength={200} onChange={(e) => editCheck(i, { name: e.target.value })} />
                        </div>
                        <div className="flex-1">
                          <label htmlFor={`check-cmd-${i}`} className="sr-only">Check {i + 1} suggested command</label>
                          <TextInput id={`check-cmd-${i}`} className="font-mono" placeholder="(no command)" value={c.suggested_command ?? ""} maxLength={300}
                            onChange={(e) => editCheck(i, { suggested_command: e.target.value || null })} />
                        </div>
                        <Button variant="ghost" aria-label={`Remove check ${i + 1}`} onClick={() => edit({ expected_checks: draft.expected_checks.filter((_, idx) => idx !== i) })}>Remove</Button>
                      </div>
                      {c.rationale && <p className="mt-1 text-xs text-slate-500">{c.rationale}</p>}
                    </li>
                  ))}
                </ul>
              </Card>

              {(save.isError || regen.isError) && <Notice tone="danger">{describeError(save.error ?? regen.error)}</Notice>}
              {saved && !dirty && <Notice tone="success">Plan saved (revision {plan.revision}).</Notice>}
              <div className="flex flex-wrap items-center gap-2">
                <Button disabled={!dirty || save.isPending} onClick={() => save.mutate(draft)}>{save.isPending ? "Saving…" : "Save plan"}</Button>
                <Button variant="secondary" disabled={!dirty} onClick={() => setDraft(plan.content)}>Discard changes</Button>
                <span className="grow" />
                {confirmRegen ? (
                  <>
                    <span className="text-sm text-slate-600">Replace the current plan{plan.edited_by_user || dirty ? " and your edits" : ""}?</span>
                    <Button variant="danger" disabled={regen.isPending} onClick={() => regen.mutate()}>{regen.isPending ? "Regenerating…" : "Yes, regenerate"}</Button>
                    <Button variant="secondary" onClick={() => setConfirmRegen(false)}>Cancel</Button>
                  </>
                ) : (
                  <Button variant="secondary" onClick={() => setConfirmRegen(true)}>Regenerate plan</Button>
                )}
              </div>
              <p className="text-xs text-slate-500">Generator: {plan.generator} v{plan.generator_version} · last updated {formatDate(plan.updated_at)}</p>
            </>
          )}
        </div>
      </div>
    </>
  );
}

function Block({ label, children }: { label: string; children: string }) {
  return (<div><dt className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</dt><dd className="mt-0.5 whitespace-pre-wrap text-slate-800">{children}</dd></div>);
}
function ListBlock({ label, items, empty }: { label: string; items: string[]; empty?: string }) {
  return (
    <div>
      <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="mt-0.5 text-slate-800">
        {items.length === 0 ? <span className="text-slate-500">{empty}</span> : <ul className="list-disc pl-5">{items.map((i) => (<li key={i}>{i}</li>))}</ul>}
      </dd>
    </div>
  );
}
