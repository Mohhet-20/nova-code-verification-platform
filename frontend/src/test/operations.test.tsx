import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, readyOk, renderApp } from "./helpers";

afterEach(() => vi.unstubAllGlobals());

const OPS = "/ops";
const page = (items: unknown[]) => ({ body: { items, total: items.length, page: 1, page_size: 10, pages: 1 } });
const overview = (over = {}) => ({
  health: { status: "ok", critical_unavailable: [], degraded: [] }, resources: { uptime_seconds: 7200, cpu_seconds: 3.2, memory_bytes: null, threads: 7 }, observed_since: "2026-10-20T10:00:00Z",
  requests_last_hour: { total: 120, errors_5xx: 2, p95_seconds: 0.25 }, jobs_last_hour: { total: 4, failed: 1, in_flight: 0 },
  workflows_last_hour: { security_scan: { succeeded: 2, failed: 1 }, indexing: { succeeded: 3 } }, open_incidents: 1,
  slos: [], notifications: { enabled: false, channels: [{ channel: "webhook", enabled: false }, { channel: "email", enabled: false, implemented: false }] },
  periodic_evaluation: { enabled: false }, limitations: ["Metrics, spans and recent logs are in memory and reset on restart."], ...over,
});
const health = { status: "ok", critical_unavailable: [], degraded: [], cached: false, checked_at: 1, dependencies: [
  { name: "database", critical: true, state: "ok", latency_ms: 1.2, detail: "reachable" }, { name: "notifications", critical: false, state: "disabled", probed: false, detail: "not configured" }] };
const signals = { signals: [{ signal: "metrics", status: "implemented", exported: "Prometheus text", limits: "in-memory" }, { signal: "traces", status: "implemented_local", exported: "optional exporter", limits: "OTLP not verified" }] };
const incident = (over = {}) => ({ id: "i1", fingerprint: "f", source: "alert", rule_id: "http_error_rate", rule_version: 1, severity: "critical", component: "api", title: "Alert: http_error_rate", summary: "http_error_rate observed 0.3 meets the condition > 0.05",
  status: "open", occurrences: 2, evidence: { observed: 0.3 }, first_seen_at: "2026-10-20T10:00:00Z", last_seen_at: "2026-10-20T10:05:00Z", acknowledged_at: null, resolved_at: null, resolution_note: null, ...over });
const base = (o = {}) => ({ "GET /ready": readyOk, [`GET ${OPS}/overview`]: { body: overview() }, [`GET ${OPS}/health`]: { body: health }, [`GET ${OPS}/signals`]: { body: signals }, ...o });
const slo = (over = {}) => ({ slo_id: "api_availability", config_version: 2, description: "Share of API requests that did not return 5xx.", sli: "availability", target: 0.995, window_minutes: 1440, effective_window_minutes: 90, window_fully_observed: false,
  status: "ok", good: 998, total: 1000, sli_value: 0.998, burn_rate: 0.4, calculation: "good = requests not answered with 5xx; total = requests", exclusions: ["health_checks"], min_samples: 30,
  limitations: ["Telemetry is held in memory and starts at process start; a restart resets history."], observed_since: "2026-10-20T10:00:00Z", error_budget: { fraction: 0.005, allowed_bad_events: 5, consumed_fraction: 0.4, remaining_fraction: 0.6 }, ...over });

describe("Operational intelligence", () => {
  it("shows measured overview, dependency state and states honestly that nothing is continuously monitored", async () => {
    mockApi(base());
    renderApp("/operations");
    const ov = await screen.findByTestId("ops-overview");
    expect(within(ov).getByText("120")).toBeInTheDocument();
    expect(within(ov).getByText("not measurable")).toBeInTheDocument();
    expect(await within(ov).findByText("database")).toBeInTheDocument();
    expect(within(ov).getByText("not probed")).toBeInTheDocument();
    expect(within(ov).getByText(/not continuously monitoring/)).toBeInTheDocument();
    expect(within(ov).getByText("email: not implemented")).toBeInTheDocument();
    expect(within(ov).getByText(/OTLP not verified/)).toBeInTheDocument();
  });

  it("shows an unavailable state when the overview cannot be loaded", async () => {
    mockApi(base({ [`GET ${OPS}/overview`]: { status: 403, body: { error: { code: "forbidden", message: "Not allowed." } } } }));
    renderApp("/operations");
    expect(await screen.findByRole("alert")).toHaveTextContent(/went wrong/i);
  });

  it("renders workflow outcomes from recorded executions and an empty state when none exist", async () => {
    mockApi(base());
    renderApp("/operations");
    await userEvent.click(await screen.findByRole("tab", { name: "Workflow performance" }));
    const table = await screen.findByTestId("workflow-table");
    expect(within(table).getByText("security scan")).toBeInTheDocument();
    expect(within(table).getAllByText("1").length).toBeGreaterThan(0);
  });

  it("shows an explicit empty state for workflows with no executions", async () => {
    mockApi(base({ [`GET ${OPS}/overview`]: { body: overview({ workflows_last_hour: {} }) } }));
    renderApp("/operations");
    await userEvent.click(await screen.findByRole("tab", { name: "Workflow performance" }));
    expect(await screen.findByText("No workflow executions recorded")).toBeInTheDocument();
  });

  it("lists incidents, shows history and runs a lifecycle action; empty list does not claim health", async () => {
    const calls = mockApi(base({
      [`GET ${OPS}/incidents`]: page([incident()]),
      [`GET ${OPS}/incidents/i1`]: { body: { ...incident(), history: [{ id: "e1", event_type: "incident_opened", summary: "x", occurred_at: "2026-10-20T10:00:00Z", note: null, severity: "critical" }], deliveries: [{ id: "d1", channel: "webhook", destination: "hooks.example.com", attempt: 1, status: "failed", http_status: 500, error_class: "server_error", created_at: "2026-10-20T10:00:01Z" }] } },
      [`POST ${OPS}/incidents/i1/acknowledge`]: { body: incident({ status: "acknowledged" }) },
    }));
    renderApp("/operations");
    await userEvent.click(await screen.findByRole("tab", { name: "Incidents & alerts" }));
    const row = await screen.findByTestId("incident-row");
    await userEvent.click(within(row).getByRole("button", { name: "Alert: http_error_rate" }));
    expect(await within(row).findByText(/opened/)).toBeInTheDocument();
    expect(within(row).getByText(/hooks.example.com: failed \(server_error\)/)).toBeInTheDocument();
    await userEvent.click(within(row).getByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(calls.calls.some((c) => c.key === `POST ${OPS}/incidents/i1/acknowledge`)).toBe(true));
  });

  it("shows an empty incident state without claiming the system is healthy, and runs evaluation with explanations", async () => {
    mockApi(base({
      [`GET ${OPS}/incidents`]: page([]), [`GET ${OPS}/diagnostics`]: { body: { findings: [], note: "An empty list means no rule matched, not that the system is healthy.", observed_since: "x" } },
      [`POST ${OPS}/alerts/evaluate`]: { body: { continuous_monitoring: false, evaluations: [
        { rule_id: "http_error_rate", rule_version: 1, description: "d", metric: "http_error_rate", window_minutes: 15, comparator: ">", threshold: 0.05, min_samples: 20, severity: "critical", status: "no_data", fired: false, observed: null, samples: 0, missing_data: true, explanation: "Not evaluated: no measurements in the window.", incident: null }] } },
    }));
    renderApp("/operations");
    await userEvent.click(await screen.findByRole("tab", { name: "Incidents & alerts" }));
    expect(await screen.findByText(/does not prove the system is healthy/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Evaluate alerts now" }));
    const table = await screen.findByTestId("eval-table");
    expect(within(table).getByText("no data")).toBeInTheDocument();
    expect(within(table).getByText(/Not evaluated: no measurements/)).toBeInTheDocument();
  });

  it("shows diagnostics as correlation with alternatives and no causal claim", async () => {
    mockApi(base({
      [`GET ${OPS}/incidents`]: page([]),
      [`GET ${OPS}/diagnostics`]: { body: { note: "n", observed_since: "x", findings: [{ rule: "db_failures_with_api_errors", title: "API 5xx responses coincide with failed 'database' probes", severity: "critical", confidence: "low", window_minutes: 60,
        signals: [{ name: "api_5xx", value: 5 }], evidence_refs: [{ type: "metric_stream", ref: "http" }], relationship: "Co-occurrence only. This is correlation, not causation.", alternatives: ["Unrelated app cause."], investigation_steps: ["Check /ops/health."], causal_claim: false }] } },
    }));
    renderApp("/operations");
    await userEvent.click(await screen.findByRole("tab", { name: "Incidents & alerts" }));
    const d = await screen.findByTestId("diagnostics");
    expect(within(d).getByText(/correlation, not causation/)).toBeInTheDocument();
    expect(within(d).getByText("low confidence")).toBeInTheDocument();
  });

  it("shows SLOs with calculation, budget and limits, and missing data as not zero failures", async () => {
    mockApi(base({ [`GET ${OPS}/slos`]: { body: { items: [slo(), slo({ slo_id: "job_completion", status: "no_data", total: 0, good: 0, sli_value: null, burn_rate: null, error_budget: null })] } } }));
    renderApp("/operations");
    await userEvent.click(await screen.findByRole("tab", { name: "SLOs" }));
    const panel = await screen.findByTestId("slo-panel");
    expect(within(panel).getByText(/a restart resets history/)).toBeInTheDocument();
    expect(within(panel).getByText("998 / 1000")).toBeInTheDocument();
    expect(within(panel).getByText(/90 of 1440 min \(not fully observed\)/)).toBeInTheDocument();
    expect(within(panel).getByText(/missing data, not zero failures/)).toBeInTheDocument();
  });

  it("renders log content and spans as plain text (no HTML injection) and handles owner-only denial", async () => {
    mockApi(base({
      [`GET ${OPS}/logs`]: { body: { buffered: 1, capacity: 2000, note: "Recent redacted records.", items: [{ ts: "2026-10-20T10:00:00Z", level: "ERROR", logger: "x", event: "unhandled_error", message: "<img src=x onerror=alert(1)>", request_id: "req_abc12345" }] } },
      [`GET ${OPS}/traces`]: { status: 403, body: { error: { code: "forbidden", message: "This operational view is restricted to the workspace owner." } } },
    }));
    const { container } = renderApp("/operations");
    await userEvent.click(await screen.findByRole("tab", { name: "Traces & logs" }));
    const logs = await screen.findByTestId("logs");
    expect(within(logs).getByText(/<img src=x onerror=alert\(1\)>/)).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
    expect(await screen.findByText(/restricted to the workspace owner/)).toBeInTheDocument();
  });
});
