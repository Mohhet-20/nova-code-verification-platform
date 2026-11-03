import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Badge, Button, ButtonLink, Card, EmptyState, ErrorState, Field, Notice, PageHeader, Pagination, Spinner, TextArea, TextInput } from "../components/ui";
import { ApiError, api, describeError } from "../services/api";
import { formatDate } from "../services/format";
import type { FailureDetail, FailureItem, Hypothesis, InvestigationDetail, LocalizationOut, VerificationItem, VerificationOutcome } from "../types/api";

type Tone = "success" | "danger" | "warning" | "info" | "neutral";
const STATUS_TONE: Record<string, Tone> = { open: "danger", investigating: "info", resolved: "success", ignored: "neutral" };
const CONF_TONE: Record<string, Tone> = { high: "success", medium: "info", low: "neutral" };
const OUTCOME_TONE: Record<VerificationOutcome, Tone> = {
  PENDING: "info", PERSISTS: "danger", NOT_REPRODUCED_IN_VERIFICATION_RUN: "success", CHANGED_FAILURE: "warning", INCONCLUSIVE: "warning",
};
const OUTCOME_LABEL: Record<VerificationOutcome, string> = {
  PENDING: "Pending", PERSISTS: "Failure persists", NOT_REPRODUCED_IN_VERIFICATION_RUN: "Not reproduced in this run",
  CHANGED_FAILURE: "A different failure", INCONCLUSIVE: "Inconclusive",
};
const CATEGORIES = ["", "assertion", "exception", "collection_error", "test_error", "unknown"];
const STATUSES = ["", "open", "investigating", "resolved", "ignored"];
const RESOLUTIONS = ["fixed_by_user", "not_a_bug", "cannot_reproduce", "wont_fix", "duplicate"];

function MutationError({ error }: { error: unknown }) {
  if (error instanceof ApiError && error.status === 403)
    return <Notice tone="warning">Your role does not permit this action. You can still read results.</Notice>;
  return <Notice tone="danger">{describeError(error)}</Notice>;
}

// ------------------------------------------------------------------------------------------------ inbox

export function FailuresPage() {
  const { repositoryId = "" } = useParams();
  const qc = useQueryClient();
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState("");
  const [category, setCategory] = useState("");
  const [search, setSearch] = useState("");
  const [paste, setPaste] = useState("");
  const [source, setSource] = useState("pytest_text");
  const repo = useQuery({ queryKey: ["repository", repositoryId], queryFn: () => api.getRepository(repositoryId) });
  const list = useQuery({
    queryKey: ["failures", repositoryId, page, status, category, search],
    queryFn: () => api.failures(repositoryId, { page, status, category, q: search }),
  });
  const ingest = useMutation({
    mutationFn: () => api.ingestFailure(repositoryId, { source, text: paste }),
    onSuccess: () => { setPaste(""); void qc.invalidateQueries({ queryKey: ["failures"] }); },
  });
  return (
    <>
      <PageHeader
        title="Failures & debugging"
        subtitle={repo.data ? `Failure reports, ranked suspect locations and approved verification runs for ${repo.data.name}. Nothing runs without your approval.` : undefined}
        actions={<ButtonLink to={`/repositories/${repositoryId}`} variant="secondary">Back to repository</ButtonLink>}
      />
      <Card title="Add a failure report">
        <div className="space-y-3">
          <Field label="Report format" htmlFor="fmt">
            <select id="fmt" value={source} onChange={(e) => setSource(e.target.value)} className="rounded border border-slate-300 px-2 py-1.5 text-sm">
              <option value="pytest_text">pytest output</option>
              <option value="python_traceback">Python traceback</option>
              <option value="junit_xml">JUnit XML</option>
            </select>
          </Field>
          <Field label="Paste the failure text" htmlFor="paste" hint="Only this text is parsed. Paths and credential-like values are removed before storage; log files are never read.">
            <TextArea id="paste" rows={5} value={paste} onChange={(e) => setPaste(e.target.value)} />
          </Field>
          <Button disabled={!paste.trim() || ingest.isPending} onClick={() => ingest.mutate()}>{ingest.isPending ? "Adding…" : "Add failure"}</Button>
          {ingest.isError && <MutationError error={ingest.error} />}
          {ingest.isSuccess && (
            <Notice tone="success">{ingest.data.created} new failure(s), {ingest.data.recurred} recurrence(s) recorded.</Notice>
          )}
        </div>
      </Card>
      <div className="mt-5" />
      <div className="mb-4 flex flex-wrap items-end gap-3">
        <Field label="Search" htmlFor="fsearch">
          <TextInput id="fsearch" value={search} onChange={(e) => { setSearch(e.target.value); setPage(1); }} placeholder="Message, test or exception" />
        </Field>
        <Field label="Status" htmlFor="fstatus">
          <select id="fstatus" value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }} className="rounded border border-slate-300 px-2 py-1.5 text-sm">
            {STATUSES.map((s) => <option key={s} value={s}>{s || "Any status"}</option>)}
          </select>
        </Field>
        <Field label="Category" htmlFor="fcat">
          <select id="fcat" value={category} onChange={(e) => { setCategory(e.target.value); setPage(1); }} className="rounded border border-slate-300 px-2 py-1.5 text-sm">
            {CATEGORIES.map((s) => <option key={s} value={s}>{s || "Any category"}</option>)}
          </select>
        </Field>
      </div>
      <Inbox repositoryId={repositoryId} list={list} page={page} onPage={setPage} filtered={Boolean(status || category || search)} />
    </>
  );
}

function Inbox({ repositoryId, list, page, onPage, filtered }: {
  repositoryId: string; list: ReturnType<typeof useQuery<import("../types/api").Page<FailureItem>>>; page: number; onPage: (p: number) => void; filtered: boolean;
}) {
  if (list.isPending) return <Spinner label="Loading failures…" />;
  if (list.isError) return <ErrorState error={list.error} onRetry={() => void list.refetch()} />;
  if (list.data.total === 0)
    return filtered ? (
      <EmptyState title="No failures match these filters">Clear a filter or change the search text.</EmptyState>
    ) : (
      <EmptyState title="No failures recorded">Paste pytest output above, or ingest a failed run from the Tests workspace. NOVA does not collect failures on its own.</EmptyState>
    );
  return (
    <section aria-label="Failure inbox">
      <ul className="divide-y divide-slate-200 rounded border border-slate-200 bg-white">
        {list.data.items.map((f) => (
          <li key={f.id} className="flex flex-wrap items-center justify-between gap-3 p-3">
            <div className="min-w-0">
              <Link className="font-medium text-sky-800 hover:underline" to={`/repositories/${repositoryId}/failures/${f.id}`}>{f.summary}</Link>
              <p className="text-xs text-slate-600">
                {f.test_id ?? "no test id"} · {f.top_location ?? "no repository location"} · last seen {formatDate(f.last_seen_at)}
              </p>
            </div>
            <div className="flex items-center gap-2">
              <Badge>{f.category}</Badge>
              <Badge tone={STATUS_TONE[f.status] ?? "neutral"}>{f.status}</Badge>
              <span className="text-xs text-slate-600">{f.occurrence_count}× seen</span>
            </div>
          </li>
        ))}
      </ul>
      <Pagination page={list.data.page} pages={list.data.pages} total={list.data.total} onPage={onPage} />
      <span className="sr-only">Page {page}</span>
    </section>
  );
}

// ------------------------------------------------------------------------------------------------ detail

export function FailureDetailPage() {
  const { repositoryId = "", failureId = "" } = useParams();
  const q = useQuery({ queryKey: ["failure", repositoryId, failureId], queryFn: () => api.failure(repositoryId, failureId) });
  return (
    <>
      <PageHeader
        title="Failure details"
        subtitle="Facts come from the stored report and the repository index. Rankings and hypotheses are labelled as such."
        actions={<ButtonLink to={`/repositories/${repositoryId}/failures`} variant="secondary">Back to failures</ButtonLink>}
      />
      {q.isPending && <Spinner label="Loading failure…" />}
      {q.isError && <ErrorState error={q.error} onRetry={() => void q.refetch()} />}
      {q.isSuccess && (
        <div className="space-y-5">
          <FailureFacts repositoryId={repositoryId} f={q.data} />
          <LocalizationPanel repositoryId={repositoryId} failureId={failureId} />
          <InvestigationWorkspace repositoryId={repositoryId} f={q.data} />
        </div>
      )}
    </>
  );
}

function FailureFacts({ repositoryId, f }: { repositoryId: string; f: FailureDetail }) {
  const qc = useQueryClient();
  const set = useMutation({
    mutationFn: (s: "open" | "ignored" | "resolved") => api.setFailureStatus(repositoryId, f.id, s),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["failure"] }),
  });
  return (
    <Card title="Recorded facts" actions={<Badge tone={STATUS_TONE[f.status] ?? "neutral"}>{f.status}</Badge>}>
      <p className="font-medium">{f.summary}</p>
      <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-slate-50 p-2 text-xs" aria-label="Sanitized error message">{f.message}</pre>
      <dl className="mt-3 grid grid-cols-2 gap-2 text-sm md:grid-cols-4">
        <div><dt className="text-slate-500">Category</dt><dd>{f.category}</dd></div>
        <div><dt className="text-slate-500">Test</dt><dd>{f.test_id ?? "unknown"}</dd></div>
        <div><dt className="text-slate-500">Seen</dt><dd>{f.occurrence_count}× since {formatDate(f.first_seen_at)}</dd></div>
        <div><dt className="text-slate-500">Source revision</dt><dd>{f.revision_known && f.revision ? f.revision.slice(0, 12) : "unknown"}</dd></div>
      </dl>
      {f.warnings.map((w) => <p key={w} className="mt-2 text-xs text-amber-800">{w}</p>)}
      <h3 className="mt-4 text-sm font-semibold">Stack frames</h3>
      {f.frames.length === 0 ? (
        <p className="text-sm text-slate-600">No stack frames could be parsed from this report.</p>
      ) : (
        <ol className="mt-1 space-y-1 text-sm">
          {f.frames.map((fr) => (
            <li key={fr.idx} className="flex flex-wrap gap-2">
              <code>{fr.display_path}{fr.line ? `:${fr.line}` : ""}</code>
              {fr.function && <span className="text-slate-600">in {fr.function}</span>}
              <Badge tone={fr.scope === "repository" ? "info" : "neutral"}>{fr.is_test ? "test" : fr.scope}</Badge>
            </li>
          ))}
        </ol>
      )}
      {f.related_findings.length > 0 && (
        <>
          <h3 className="mt-4 text-sm font-semibold">Static-analysis findings on the same files</h3>
          <ul className="text-sm">
            {f.related_findings.map((x) => <li key={x.id}>{x.title} <span className="text-slate-600">({x.rel_path}{x.start_line ? `:${x.start_line}` : ""})</span></li>)}
          </ul>
        </>
      )}
      <div className="mt-4 flex gap-2">
        {f.status !== "ignored" && <Button variant="secondary" disabled={set.isPending} onClick={() => set.mutate("ignored")}>Ignore</Button>}
        {f.status !== "open" && <Button variant="secondary" disabled={set.isPending} onClick={() => set.mutate("open")}>Reopen</Button>}
      </div>
      {set.isError && <MutationError error={set.error} />}
    </Card>
  );
}

function LocalizationPanel({ repositoryId, failureId }: { repositoryId: string; failureId: string }) {
  const q = useQuery({ queryKey: ["localization", repositoryId, failureId], queryFn: () => api.localization(repositoryId, failureId) });
  return (
    <Card title="Suspect locations (heuristic ranking)">
      {q.isPending && <Spinner label="Ranking locations…" />}
      {q.isError && <ErrorState error={q.error} onRetry={() => void q.refetch()} />}
      {q.isSuccess && <LocalizationBody l={q.data} />}
    </Card>
  );
}

function LocalizationBody({ l }: { l: LocalizationOut }) {
  return (
    <div className="space-y-3">
      <Notice tone="info">{l.interpretation}</Notice>
      {l.stale && <Notice tone="warning">The repository index changed since this failure was recorded; rankings may be out of date.</Notice>}
      {l.state === "insufficient_evidence" ? (
        <Notice tone="warning">Insufficient evidence to rank a location. NOVA does not guess.</Notice>
      ) : (
        <ol className="space-y-3">
          {l.candidates.map((c) => (
            <li key={c.rank} className="rounded border border-slate-200 p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold">#{c.rank}</span>
                <code>{c.symbol ?? c.rel_path}</code>
                <span className="text-xs text-slate-600">{c.rel_path}{c.start_line ? `:${c.start_line}-${c.end_line}` : ""}</span>
                <Badge tone={CONF_TONE[c.confidence]}>{c.confidence} confidence</Badge>
                <span className="text-xs text-slate-600">score {c.score} · {c.role.replace(/_/g, " ")}</span>
              </div>
              <p className="mt-1 text-sm">{c.explanation}</p>
              {c.contradicting.length > 0 && (
                <ul className="mt-1 text-xs text-amber-800">{c.contradicting.map((e) => <li key={e.id}>Against: {e.detail}</li>)}</ul>
              )}
            </li>
          ))}
        </ol>
      )}
      {l.missing_evidence.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold">Missing evidence</h3>
          <ul className="list-disc pl-5 text-sm text-slate-700">{l.missing_evidence.map((m) => <li key={m}>{m}</li>)}</ul>
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ investigation

function InvestigationWorkspace({ repositoryId, f }: { repositoryId: string; f: FailureDetail }) {
  const qc = useQueryClient();
  const create = useMutation({
    mutationFn: () => api.createInvestigation(repositoryId, f.id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["failure"] }),
  });
  if (!f.investigation_id || f.investigation_status === "closed")
    return (
      <Card title="Investigation">
        <p className="text-sm text-slate-700">Start an investigation to get observations, labelled hypotheses, missing information and verification steps.</p>
        <Button className="mt-3" disabled={create.isPending} onClick={() => create.mutate()}>{create.isPending ? "Creating…" : "Start investigation"}</Button>
        {create.isError && <MutationError error={create.error} />}
      </Card>
    );
  return <InvestigationBody repositoryId={repositoryId} investigationId={f.investigation_id} />;
}

function InvestigationBody({ repositoryId, investigationId }: { repositoryId: string; investigationId: string }) {
  const q = useQuery({ queryKey: ["investigation", repositoryId, investigationId], queryFn: () => api.investigation(repositoryId, investigationId) });
  if (q.isPending) return <Spinner label="Loading investigation…" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  return <InvestigationView repositoryId={repositoryId} inv={q.data} />;
}

function InvestigationView({ repositoryId, inv }: { repositoryId: string; inv: InvestigationDetail }) {
  const qc = useQueryClient();
  const c = inv.content;
  const [notes, setNotes] = useState(inv.notes);
  const [resolution, setResolution] = useState(RESOLUTIONS[0]);
  const refresh = () => { void qc.invalidateQueries({ queryKey: ["investigation"] }); void qc.invalidateQueries({ queryKey: ["failure"] }); };
  const save = useMutation({ mutationFn: () => api.patchInvestigation(repositoryId, inv.id, { notes }), onSuccess: refresh });
  const close = useMutation({ mutationFn: () => api.patchInvestigation(repositoryId, inv.id, { status: "closed", resolution }), onSuccess: refresh });
  return (
    <Card title="Investigation workspace" actions={<Badge tone="info">{inv.status}</Badge>}>
      <div className="space-y-5">
        <section aria-label="Observed facts">
          <h3 className="text-sm font-semibold">Facts <Badge tone="success">observed</Badge></h3>
          <ul className="list-disc pl-5 text-sm">{c.observations.map((o) => <li key={o.text}>{o.text}</li>)}</ul>
        </section>
        <section aria-label="Hypotheses">
          <h3 className="text-sm font-semibold">Hypotheses <Badge tone="warning">unverified</Badge></h3>
          <p className="text-xs text-slate-600">Generated from templates and the evidence above. None of these has been confirmed.</p>
          <div className="mt-2 space-y-3">{c.hypotheses.map((h) => <HypothesisCard key={h.id} h={h} />)}</div>
        </section>
        {c.missing_information.length > 0 && (
          <section aria-label="Missing information">
            <h3 className="text-sm font-semibold">Missing information</h3>
            <ul className="list-disc pl-5 text-sm">{c.missing_information.map((m) => <li key={m}>{m}</li>)}</ul>
          </section>
        )}
        {c.next_tests.length > 0 && (
          <section aria-label="Suggested next tests">
            <h3 className="text-sm font-semibold">Suggested next tests</h3>
            <ul className="list-disc pl-5 text-sm">{c.next_tests.map((m) => <li key={m}>{m}</li>)}</ul>
          </section>
        )}
        {c.impact && (
          <section aria-label="Dependency impact">
            <h3 className="text-sm font-semibold">Possible impact of {c.impact.rel_path}</h3>
            {c.impact.dependents.length === 0 ? (
              <p className="text-sm text-slate-600">{c.impact.graph_available ? "No dependents were found in the index." : "Dependency data is unavailable, so impact is unknown."}</p>
            ) : (
              <ul className="text-sm">{c.impact.dependents.map((d) => (
                <li key={d.rel_path}>{d.rel_path} · depth {d.depth} · <Badge tone={d.relationship === "confirmed_import" ? "success" : "neutral"}>{d.relationship.replace(/_/g, " ")}</Badge></li>
              ))}</ul>
            )}
            {c.impact.limitations.map((x) => <p key={x} className="text-xs text-slate-600">{x}</p>)}
          </section>
        )}
        <VerificationSection repositoryId={repositoryId} inv={inv} />
        <section aria-label="Notes">
          <Field label="Your notes" htmlFor="notes" hint="Credential-like values are removed on save.">
            <TextArea id="notes" rows={3} value={notes} onChange={(e) => setNotes(e.target.value)} />
          </Field>
          <Button variant="secondary" disabled={save.isPending || notes === inv.notes} onClick={() => save.mutate()}>Save notes</Button>
          {save.isError && <MutationError error={save.error} />}
        </section>
        <section aria-label="Close investigation" className="flex flex-wrap items-end gap-2">
          <Field label="Resolution (your decision)" htmlFor="res">
            <select id="res" value={resolution} onChange={(e) => setResolution(e.target.value)} className="rounded border border-slate-300 px-2 py-1.5 text-sm">
              {RESOLUTIONS.map((r) => <option key={r} value={r}>{r.replace(/_/g, " ")}</option>)}
            </select>
          </Field>
          <Button variant="secondary" disabled={close.isPending} onClick={() => close.mutate()}>Close investigation</Button>
          {close.isError && <MutationError error={close.error} />}
        </section>
        <details className="text-xs text-slate-600">
          <summary>Limitations</summary>
          <ul className="list-disc pl-5">{c.limitations.map((x) => <li key={x}>{x}</li>)}</ul>
        </details>
      </div>
    </Card>
  );
}

function HypothesisCard({ h }: { h: Hypothesis }) {
  return (
    <article className="rounded border border-amber-200 bg-amber-50 p-3 text-sm" aria-label={`Hypothesis ${h.id}`}>
      <p><Badge tone="warning">hypothesis</Badge> {h.statement}</p>
      {h.supporting.length > 0 && <ul className="mt-1 list-disc pl-5 text-xs"><li className="list-none font-medium">Supporting</li>{h.supporting.map((s) => <li key={s.text}>{s.text}</li>)}</ul>}
      {h.contradicting.length > 0 && <ul className="mt-1 list-disc pl-5 text-xs"><li className="list-none font-medium">Contradicting</li>{h.contradicting.map((s) => <li key={s.text}>{s.text}</li>)}</ul>}
      {h.verification.length > 0 && <p className="mt-1 text-xs text-slate-700">To verify: {h.verification.join(" ")}</p>}
    </article>
  );
}

// ------------------------------------------------------------------------------------------------ verification

function VerificationSection({ repositoryId, inv }: { repositoryId: string; inv: InvestigationDetail }) {
  const qc = useQueryClient();
  const list = useQuery({
    queryKey: ["verifications", repositoryId, inv.id],
    queryFn: () => api.verifications(repositoryId, inv.id),
    refetchInterval: (query) => (query.state.data?.items.some((v) => v.outcome === "PENDING") ? 2000 : false),
  });
  const preview = useMutation({ mutationFn: () => api.previewVerification(repositoryId, inv.id) });
  const start = useMutation({
    mutationFn: () => api.startVerification(repositoryId, inv.id),
    onSuccess: () => { preview.reset(); void qc.invalidateQueries({ queryKey: ["verifications"] }); void qc.invalidateQueries({ queryKey: ["failure"] }); },
  });
  return (
    <section aria-label="Verification">
      <h3 className="text-sm font-semibold">Verification</h3>
      <p className="text-xs text-slate-600">A verification reruns the selected tests after you approve it. A passing run means “not reproduced in that run”, never “fixed”.</p>
      {!preview.data && <Button className="mt-2" variant="secondary" disabled={preview.isPending} onClick={() => preview.mutate()}>Review verification run</Button>}
      {preview.isError && <MutationError error={preview.error} />}
      {preview.data && (
        <div className="mt-2 space-y-2 rounded border border-slate-300 p-3 text-sm">
          <p className="font-medium">Nothing has run yet. Approving will execute:</p>
          <ul className="list-disc pl-5">{preview.data.targets.map((t) => <li key={t}><code>{t}</code></li>)}</ul>
          <p className="text-xs text-slate-700">{preview.data.execution}</p>
          <div className="flex gap-2">
            <Button disabled={start.isPending} onClick={() => start.mutate()}>{start.isPending ? "Starting…" : "Approve and run"}</Button>
            <Button variant="secondary" onClick={() => preview.reset()}>Cancel</Button>
          </div>
        </div>
      )}
      {start.isError && <MutationError error={start.error} />}
      {list.isPending && <Spinner label="Loading verification results…" />}
      {list.isError && <ErrorState error={list.error} onRetry={() => void list.refetch()} />}
      {list.isSuccess && list.data.items.length === 0 && <p className="mt-2 text-sm text-slate-600">No verification has been run.</p>}
      {list.isSuccess && list.data.items.map((v) => <VerificationCard key={v.id} v={v} />)}
    </section>
  );
}

function RunCell({ title, run }: { title: string; run: VerificationItem["verification_run"] }) {
  if (!run) return <div><h4 className="font-medium">{title}</h4><p className="text-slate-600">Not available.</p></div>;
  return (
    <div>
      <h4 className="font-medium">{title}</h4>
      <p>Status: {run.status}</p>
      <p>{run.tests_passed ?? 0} passed · {run.tests_failed ?? 0} failed · {run.tests_errors ?? 0} errors · {run.tests_skipped ?? 0} skipped</p>
    </div>
  );
}

function VerificationCard({ v }: { v: VerificationItem }) {
  return (
    <article className="mt-3 rounded border border-slate-200 p-3 text-sm" aria-label={`Verification ${v.outcome}`}>
      <div className="flex items-center gap-2">
        <Badge tone={OUTCOME_TONE[v.outcome]}>{OUTCOME_LABEL[v.outcome]}</Badge>
        <span className="text-xs text-slate-600">{formatDate(v.created_at)}</span>
      </div>
      <p className="mt-1">{v.meaning}</p>
      {v.reason && v.reason !== v.meaning && <p className="text-xs text-slate-700">{v.reason}</p>}
      <div className="mt-2 grid grid-cols-2 gap-3">
        <RunCell title="Original run" run={v.original_run} />
        <RunCell title="Verification run" run={v.verification_run} />
      </div>
    </article>
  );
}
