import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Badge, Button, Card, EmptyState, ErrorState, Field, Notice, Pagination, Spinner, TextArea, TextInput } from "../components/ui";
import { api } from "../services/api";
import { formatDate } from "../services/format";
import type { SecEvaluation, SecScan } from "../types/api";
import { MutationError } from "./RepairsPage";
import { DecisionBadge, SeverityBadge } from "./SecurityShared";

export function InventoryPanel({ repositoryId, scan }: { repositoryId: string; scan: SecScan | null }) {
  const [q, setQ] = useState("");
  const [assessment, setAssessment] = useState("");
  const [page, setPage] = useState(1);
  const list = useQuery({ queryKey: ["sec-components", repositoryId, scan?.id, q, assessment, page], queryFn: () => api.secComponents(repositoryId, scan?.id ?? "", { q: q || undefined, assessment: assessment || undefined, page }), enabled: !!scan });
  if (!scan) return <EmptyState title="No scan yet">Run a security scan to build the dependency inventory.</EmptyState>;
  const unsupported = ((scan.coverage?.inventory as { unsupported?: { path: string; detail: string }[] } | undefined)?.unsupported ?? []);
  return (
    <Card title="Dependency inventory">
      <p className="mb-2 text-xs text-slate-600">Read from manifests and lockfiles only: nothing is installed or executed. “No known advisory” is a statement about the queried sources, not a safety claim.</p>
      {unsupported.length > 0 && <Notice tone="warning">Not covered (unsupported manifests): {unsupported.map((u) => u.path).join(", ")}</Notice>}
      <div className="my-3 flex flex-wrap items-end gap-3">
        <Field label="Package name" htmlFor="inv-q"><TextInput id="inv-q" value={q} onChange={(e) => { setQ(e.target.value); setPage(1); }} /></Field>
        <Field label="Assessment" htmlFor="inv-a">
          <select id="inv-a" value={assessment} onChange={(e) => { setAssessment(e.target.value); setPage(1); }} className="rounded border border-slate-300 p-1.5 text-sm">
            <option value="">all</option><option value="confirmed">confirmed</option><option value="possible">possible</option><option value="unassessed">unassessed</option><option value="no_known_advisory">no known advisory</option>
          </select>
        </Field>
      </div>
      {list.isLoading ? <Spinner /> : list.error ? <ErrorState error={list.error} /> : !list.data || list.data.items.length === 0 ? <EmptyState title="No components">No supported manifests produced components.</EmptyState> : (
        <>
          <ul className="divide-y divide-slate-100" aria-label="Components">
            {list.data.items.map((c) => (
              <li key={c.id} className="flex flex-wrap items-center gap-2 py-2 text-sm" data-testid={`comp-${c.name}`}>
                <span className="font-medium">{c.name}</span><code className="text-xs">{c.version ?? "version unknown"}</code>
                <Badge>{c.ecosystem}</Badge><Badge tone={c.relationship === "direct" ? "info" : "neutral"}>{c.relationship}</Badge><Badge>{c.scope}</Badge>
                <Badge tone={c.assessment?.status === "confirmed" || c.assessment?.status === "possible" ? "danger" : c.assessment?.status === "unassessed" ? "warning" : "neutral"}>{(c.assessment?.status ?? "not assessed").replace(/_/g, " ")}</Badge>
                {c.notes.length > 0 && <span className="text-xs text-slate-500">{c.notes[0]}</span>}
              </li>
            ))}
          </ul>
          <Pagination page={list.data.page} pages={list.data.pages} total={list.data.total} onPage={setPage} />
        </>
      )}
    </Card>
  );
}

export function AdvisoriesPanel({ repositoryId }: { repositoryId: string }) {
  const m = useQuery({ queryKey: ["sec-advisories", repositoryId], queryFn: () => api.secAdvisoryMatches(repositoryId) });
  if (m.isLoading) return <Spinner />;
  if (m.error || !m.data) return <ErrorState error={m.error} />;
  return (
    <Card title="Advisory matches">
      <p className="mb-2 text-xs text-slate-600">A match is <b>confirmed</b> when the installed version is in an advisory’s affected range and <b>possible</b> when the version or range could not be evaluated. Exploitability in this repository is not assessed.</p>
      {m.data.items.length === 0 ? <EmptyState title="No advisory matches">No match was recorded. This is not a statement that dependencies are safe: see scan coverage for what could be assessed.</EmptyState> : (
        <ul className="divide-y divide-slate-100" aria-label="Advisory matches">
          {m.data.items.map((f) => (
            <li key={f.id} className="space-y-1 py-2 text-sm" data-testid={`adv-${f.id}`}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{f.title}</span><SeverityBadge severity={f.severity} />
                <Badge tone={f.match_status === "confirmed" ? "danger" : "warning"}>{f.match_status}</Badge>
                {f.evidence.severity_source === "unspecified" && <Badge>severity not provided by source</Badge>}
                <Badge tone={f.status === "open" ? "warning" : "neutral"}>{f.status.replace("_", " ")}</Badge>
              </div>
              <p className="text-xs text-slate-600">{f.evidence.reason}</p>
              <ul className="text-xs text-slate-600">
                {(f.evidence.sources ?? []).map((s) => <li key={`${s.provider}-${s.advisory_id}`}>{s.advisory_id} via <b>{s.provider}</b>{s.retrieved_at ? `, retrieved ${formatDate(s.retrieved_at)}` : ""}</li>)}
              </ul>
              <p className="text-xs">Fixed in: {(f.evidence.fixed_versions ?? []).join(", ") || "no fixed version recorded"}</p>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function EvaluationView({ d }: { d: SecEvaluation }) {
  return (
    <div className="space-y-2 text-sm" data-testid="evaluation">
      <div className="flex flex-wrap items-center gap-2"><DecisionBadge status={d.status} /><span className="text-xs text-slate-500">policy v{d.policy_version} · {formatDate(d.evaluated_at)} · revision {d.revision.head_sha?.slice(0, 8) ?? "unknown"}{d.revision.dirty ? " (uncommitted changes)" : ""}</span></div>
      <p>{d.explanation}</p>
      {d.missing_data.length > 0 && <Notice tone="warning">Missing data: {d.missing_data.join("; ")}</Notice>}
      {d.skipped_checks.length > 0 && <p className="text-xs text-slate-600">Skipped checks: {d.skipped_checks.join("; ")}</p>}
      <ul className="divide-y divide-slate-100 text-xs">{d.checks.map((c) => <li key={c.id} className="py-1"><Badge tone={c.status === "pass" ? "success" : c.status === "fail" ? "danger" : "warning"}>{c.status}</Badge> <b>{c.id}</b>: {c.detail}</li>)}</ul>
      {d.causes.length > 0 && <div><p className="font-medium">Findings that caused this decision</p><ul className="text-xs">{d.causes.slice(0, 20).map((c) => <li key={c.finding_id}>{c.severity} · {c.title} <span className="text-slate-500">({c.why})</span></li>)}</ul></div>}
      {d.exceptions_applied.length > 0 && <p className="text-xs">Exceptions applied: {d.exceptions_applied.map((e) => `${e.reason} (until ${formatDate(e.expires_at)})`).join("; ")}</p>}
      {d.expired_exceptions.length > 0 && <Notice tone="warning">{d.expired_exceptions.length} expired exception(s) no longer apply.</Notice>}
      <p className="text-xs text-slate-500">{d.meaning}</p>
    </div>
  );
}

export function PolicyPanel({ repositoryId, canAdmin, canWrite }: { repositoryId: string; canAdmin: boolean; canWrite: boolean }) {
  const qc = useQueryClient();
  const policy = useQuery({ queryKey: ["sec-policy", repositoryId], queryFn: () => api.secPolicy(repositoryId) });
  const supps = useQuery({ queryKey: ["sec-supp", repositoryId], queryFn: () => api.secSuppressions(repositoryId) });
  const exceptions = useQuery({ queryKey: ["sec-exc", repositoryId], queryFn: () => api.secExceptions(repositoryId) });
  const [draft, setDraft] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [result, setResult] = useState<SecEvaluation | null>(null);
  const [xType, setXType] = useState("rule");
  const [xValue, setXValue] = useState("");
  const [xReason, setXReason] = useState("");
  const [xDays, setXDays] = useState("30");
  const save = useMutation({ mutationFn: () => api.secPutPolicy(repositoryId, JSON.parse(draft ?? "{}") as Record<string, unknown>, note), onSuccess: () => { setDraft(null); void qc.invalidateQueries({ queryKey: ["sec-policy", repositoryId] }); void qc.invalidateQueries({ queryKey: ["sec-overview", repositoryId] }); } });
  const evaluate = useMutation({ mutationFn: () => api.secEvaluate(repositoryId), onSuccess: (d) => { setResult(d); void qc.invalidateQueries({ queryKey: ["sec-overview", repositoryId] }); } });
  const addX = useMutation({
    mutationFn: () => api.secAddException(repositoryId, { selector: { type: xType, value: xValue }, reason: xReason, expires_at: new Date(Date.now() + Number(xDays) * 86400000).toISOString() }),
    onSuccess: () => { setXValue(""); setXReason(""); void qc.invalidateQueries({ queryKey: ["sec-exc", repositoryId] }); },
  });
  const revokeX = useMutation({ mutationFn: (id: string) => api.secRevokeException(repositoryId, id), onSuccess: () => void qc.invalidateQueries({ queryKey: ["sec-exc", repositoryId] }) });
  const revokeS = useMutation({ mutationFn: (id: string) => api.secRevokeSuppression(repositoryId, id, "revoked from the workspace"), onSuccess: () => { void qc.invalidateQueries({ queryKey: ["sec-supp", repositoryId] }); void qc.invalidateQueries({ queryKey: ["sec-findings"] }); } });
  if (policy.isLoading) return <Spinner />;
  if (policy.error || !policy.data) return <ErrorState error={policy.error} />;
  const p = policy.data;
  let invalid = false;
  if (draft !== null) { try { JSON.parse(draft); } catch { invalid = true; } }
  return (
    <div className="space-y-4">
      <Card title="Policy gate" actions={canWrite ? <Button disabled={evaluate.isPending} onClick={() => evaluate.mutate()}>{evaluate.isPending ? "Evaluating…" : "Evaluate latest scan"}</Button> : undefined}>
        {result ? <EvaluationView d={result} /> : <p className="text-sm text-slate-600">Evaluate the latest scan against policy v{p.version}{p.is_default ? " (built-in default)" : ""}. Pass, fail and indeterminate are distinct: missing or partial evidence is never a pass.</p>}
        {evaluate.error && <MutationError error={evaluate.error} />}
      </Card>
      <Card title={`Policy rules (v${p.version})`}>
        <pre className="max-h-64 overflow-auto rounded bg-slate-50 p-2 text-xs" data-testid="policy-json">{JSON.stringify(p.rules, null, 2)}</pre>
        {canAdmin ? (
          draft === null ? <Button variant="secondary" className="mt-2" onClick={() => setDraft(JSON.stringify(p.rules, null, 2))}>Edit policy</Button> : (
            <div className="mt-2 space-y-2">
              <Field label="Rules (JSON)" htmlFor="pol-json"><TextArea id="pol-json" rows={10} value={draft} onChange={(e) => setDraft(e.target.value)} className="w-full font-mono text-xs" /></Field>
              <Field label="Change note" htmlFor="pol-note"><TextInput id="pol-note" value={note} onChange={(e) => setNote(e.target.value)} /></Field>
              {invalid && <Notice tone="warning">The rules are not valid JSON.</Notice>}
              <div className="flex gap-2"><Button disabled={invalid || save.isPending} onClick={() => save.mutate()}>Save as new version</Button><Button variant="secondary" onClick={() => setDraft(null)}>Cancel</Button></div>
              {save.error && <MutationError error={save.error} />}
            </div>
          )
        ) : <Notice>Only a workspace owner can change the security policy.</Notice>}
      </Card>
      <Card title="Suppressions">
        {!supps.data || supps.data.length === 0 ? <EmptyState title="No suppressions">Suppress a finding from its detail view; a reason is required and suppressed findings stay visible.</EmptyState> : (
          <ul className="divide-y divide-slate-100 text-sm">{supps.data.map((s) => (
            <li key={s.id} className="flex flex-wrap items-center gap-2 py-2" data-testid={`supp-${s.id}`}>
              <Badge tone={s.state === "active" ? "info" : "neutral"}>{s.state}</Badge><span>{s.kind === "finding" ? "one finding" : `${s.rule_id} in ${s.path_glob}`}</span>
              <span className="text-xs text-slate-600">{s.reason}{s.expires_at ? ` · expires ${formatDate(s.expires_at)}` : " · no expiry"}</span>
              {canWrite && s.state === "active" && <Button variant="secondary" disabled={revokeS.isPending} onClick={() => revokeS.mutate(s.id)}>Revoke</Button>}
            </li>))}</ul>
        )}
      </Card>
      <Card title="Policy exceptions (always expire)">
        {!exceptions.data || exceptions.data.length === 0 ? <EmptyState title="No exceptions">Exceptions are listed in every evaluation that uses them.</EmptyState> : (
          <ul className="divide-y divide-slate-100 text-sm">{exceptions.data.map((x) => (
            <li key={x.id} className="flex flex-wrap items-center gap-2 py-2" data-testid={`exc-${x.id}`}>
              <Badge tone={x.state === "active" ? "info" : "neutral"}>{x.state}</Badge><code className="text-xs">{x.selector.type}:{x.selector.value}</code><span className="text-xs text-slate-600">{x.reason} · until {formatDate(x.expires_at)}</span>
              {canAdmin && x.state === "active" && <Button variant="secondary" disabled={revokeX.isPending} onClick={() => revokeX.mutate(x.id)}>Revoke</Button>}
            </li>))}</ul>
        )}
        {canAdmin && (
          <form className="mt-3 grid gap-2 sm:grid-cols-2" onSubmit={(e) => { e.preventDefault(); addX.mutate(); }}>
            <Field label="Selector type" htmlFor="x-type"><select id="x-type" value={xType} onChange={(e) => setXType(e.target.value)} className="w-full rounded border border-slate-300 p-1.5 text-sm"><option value="rule">rule</option><option value="advisory">advisory</option><option value="package">package</option><option value="finding">finding</option></select></Field>
            <Field label="Value" htmlFor="x-value"><TextInput id="x-value" value={xValue} onChange={(e) => setXValue(e.target.value)} /></Field>
            <Field label="Reason" htmlFor="x-reason"><TextInput id="x-reason" value={xReason} onChange={(e) => setXReason(e.target.value)} /></Field>
            <Field label="Expires in (days, max 365)" htmlFor="x-days"><TextInput id="x-days" type="number" value={xDays} onChange={(e) => setXDays(e.target.value)} /></Field>
            <div className="sm:col-span-2"><Button type="submit" disabled={addX.isPending || !xValue || xReason.length < 8}>Grant exception</Button></div>
          </form>
        )}
        {addX.error && <MutationError error={addX.error} />}
      </Card>
    </div>
  );
}
