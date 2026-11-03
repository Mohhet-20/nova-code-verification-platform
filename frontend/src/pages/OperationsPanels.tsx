import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Badge, Card, EmptyState, ErrorState, Notice, Spinner } from "../components/ui";
import { api } from "../services/api";
import { formatDate } from "../services/format";

type Tone = "success" | "danger" | "warning" | "info" | "neutral";
export const stateTone = (s: string): Tone => (s === "ok" || s === "ready" ? "success" : s === "degraded" || s === "disabled" ? "warning" : s === "unavailable" ? "danger" : "neutral");
const SLO_TONE: Record<string, Tone> = { ok: "success", at_risk: "warning", exhausted: "danger", insufficient_data: "neutral", no_data: "neutral" };
const pct = (v: number | null) => (v == null ? "–" : `${(v * 100).toFixed(2)}%`);

export function SloPanel() {
  const q = useQuery({ queryKey: ["ops-slos"], queryFn: api.opsSlos, refetchInterval: 60_000 });
  if (q.isPending) return <Spinner label="Calculating SLOs…" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  return (
    <div className="space-y-4" data-testid="slo-panel">
      <Notice tone="warning">{q.data.items[0]?.limitations.join(" ") ?? "SLOs use in-memory telemetry that resets on restart."}</Notice>
      <div className="grid gap-4 md:grid-cols-2">
        {q.data.items.map((s) => (
          <Card key={s.slo_id} title={<span>{s.slo_id.replace(/_/g, " ")} <span className="text-xs font-normal text-slate-500">config v{s.config_version}</span></span>}>
            <div className="mb-2 flex flex-wrap items-center gap-2"><Badge tone={SLO_TONE[s.status]}>{s.status.replace("_", " ")}</Badge><span className="text-sm">SLI {pct(s.sli_value)} / target {pct(s.target)}</span></div>
            <p className="text-sm text-slate-700">{s.description}</p>
            {s.total > 0 ? (
              <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-sm">
                <dt className="text-slate-500">Good / total</dt><dd>{s.good} / {s.total}</dd>
                <dt className="text-slate-500">Budget consumed</dt><dd>{s.error_budget?.consumed_fraction == null ? "exceeded" : pct(s.error_budget.consumed_fraction)}</dd>
                <dt className="text-slate-500">Burn rate</dt><dd>{s.burn_rate == null ? "–" : s.burn_rate.toFixed(2)}×</dd>
                <dt className="text-slate-500">Window</dt><dd>{s.effective_window_minutes} of {s.window_minutes} min{s.window_fully_observed ? "" : " (not fully observed)"}</dd>
              </dl>
            ) : <p className="mt-2 text-sm text-slate-600">No qualifying events yet. This is missing data, not zero failures.</p>}
            {s.note && s.total > 0 && <p className="mt-1 text-xs text-amber-800">{s.note}</p>}
            <details className="mt-2 text-xs text-slate-600"><summary className="cursor-pointer">How this is calculated</summary><p className="mt-1">{s.calculation}. Exclusions: {s.exclusions.length ? s.exclusions.join(", ") : "none"}. Minimum samples: {s.min_samples}. Telemetry since {formatDate(s.observed_since)}.</p></details>
          </Card>
        ))}
      </div>
    </div>
  );
}

export function DiagnosticsPanel() {
  const q = useQuery({ queryKey: ["ops-diagnostics"], queryFn: api.opsDiagnostics });
  return (
    <Card title="Reliability diagnostics">
      {q.isPending ? <Spinner /> : q.isError ? <ErrorState error={q.error} /> : q.data.findings.length === 0 ? (
        <EmptyState title="No diagnostic rule matched">{q.data.note}</EmptyState>
      ) : (
        <ul className="space-y-3" data-testid="diagnostics">{q.data.findings.map((f, i) => (
          <li key={`${f.rule}-${i}`} className="rounded border border-slate-200 p-3 text-sm">
            <div className="flex flex-wrap items-center gap-2"><Badge tone={f.severity === "critical" ? "danger" : "warning"}>{f.severity}</Badge><Badge>{f.confidence} confidence</Badge><b>{f.title}</b></div>
            <p className="mt-1 text-slate-700">{f.relationship}</p>
            <p className="mt-1 text-xs text-slate-600">Signals: {f.signals.map((s) => `${s.name}=${String(s.value)}`).join(", ")} · window {f.window_minutes} min · evidence: {f.evidence_refs.map((e) => `${e.type}:${e.ref}`).join(", ")}</p>
            <details className="mt-1"><summary className="cursor-pointer text-xs">Alternative explanations and next steps</summary>
              <ul className="list-disc pl-5 text-xs">{f.alternatives.map((a) => <li key={a}>Alternative: {a}</li>)}{f.investigation_steps.map((a) => <li key={a}>Check: {a}</li>)}</ul></details>
          </li>))}</ul>
      )}
    </Card>
  );
}

export function InvestigationPanel() {
  const [rid, setRid] = useState("");
  const [level, setLevel] = useState("");
  const [text, setText] = useState("");
  const logs = useQuery({ queryKey: ["ops-logs", rid, level, text], queryFn: () => api.opsLogs({ request_id: rid || undefined, level: level || undefined, q: text || undefined }) });
  const traces = useQuery({ queryKey: ["ops-traces", rid], queryFn: () => api.opsTraces({ request_id: rid || undefined }) });
  return (
    <div className="space-y-4">
      <Card title="Filter">
        <div className="flex flex-wrap gap-3 text-sm">
          <label className="flex flex-col gap-1">Request ID<input value={rid} onChange={(e) => setRid(e.target.value.trim())} maxLength={64} className="rounded border border-slate-300 px-2 py-1" /></label>
          <label className="flex flex-col gap-1">Level<select value={level} onChange={(e) => setLevel(e.target.value)} className="rounded border border-slate-300 px-2 py-1"><option value="">any</option>{["ERROR", "WARNING", "INFO", "DEBUG"].map((l) => <option key={l}>{l}</option>)}</select></label>
          <label className="flex flex-col gap-1">Message contains<input value={text} onChange={(e) => setText(e.target.value)} maxLength={100} className="rounded border border-slate-300 px-2 py-1" /></label>
        </div>
        <p className="mt-2 text-xs text-slate-500">Owner only. Content is shown as plain text; credentials are redacted before storage.</p>
      </Card>
      <Card title="Spans">
        {traces.isPending ? <Spinner /> : traces.isError ? <ErrorState error={traces.error} /> : traces.data.items.length === 0 ? (
          <EmptyState title="No spans recorded">Spans are held in memory (sampling ratio {traces.data.sample_ratio}) and reset on restart.</EmptyState>
        ) : (
          <table className="w-full text-sm" data-testid="spans"><thead className="text-left text-xs text-slate-500"><tr><th>Span</th><th>Status</th><th>Duration</th><th>Trace</th><th>Request</th></tr></thead>
            <tbody>{traces.data.items.map((s) => <tr key={s.span_id} className="border-t border-slate-100"><td className="py-1">{s.name}{s.attributes["http.route"] ? ` ${String(s.attributes["http.route"])}` : ""}</td><td>{s.status}</td><td>{s.duration_ms} ms</td><td><code className="text-xs">{s.trace_id.slice(0, 8)}</code></td><td><code className="text-xs">{s.request_id ?? "–"}</code></td></tr>)}</tbody></table>
        )}
      </Card>
      <Card title="Recent logs">
        {logs.isPending ? <Spinner /> : logs.isError ? <ErrorState error={logs.error} /> : logs.data.items.length === 0 ? (
          <EmptyState title="No matching log records">{logs.data.note}</EmptyState>
        ) : (
          <ul className="space-y-1 font-mono text-xs" data-testid="logs">{logs.data.items.map((r, i) => (
            <li key={`${r.ts}-${i}`} className="border-b border-slate-100 pb-1"><Badge tone={r.level === "ERROR" ? "danger" : r.level === "WARNING" ? "warning" : "neutral"}>{r.level}</Badge> {r.ts} {r.event ? `[${r.event}] ` : ""}{r.message}{r.request_id ? ` · req ${r.request_id}` : ""}{r.error_class ? ` · ${r.error_class}` : ""}</li>))}</ul>
        )}
        {logs.data && <p className="mt-2 text-xs text-slate-500">{logs.data.buffered} of {logs.data.capacity} buffered records. {logs.data.note}</p>}
      </Card>
    </div>
  );
}
