import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, readyOk, renderApp } from "./helpers";
import { repoDetail } from "./fixtures";

afterEach(() => vi.unstubAllGlobals());

const BASE = "/repositories/r1";
const run = (over: Record<string, unknown> = {}) => ({
  id: "run1", repository_id: "r1", index_snapshot_id: "s1", status: "completed", mode: "incremental", started_at: "2026-10-18T10:00:00Z",
  finished_at: "2026-10-18T10:00:03Z", duration_ms: 3000, files_eligible: 10, files_analyzed: 9, files_unchanged: 0, files_excluded: 1,
  files_skipped: 0, files_parse_failed: 0, files_failed: 0, files_not_reached: 0, findings_new: 2, findings_reopened: 0, findings_resolved: 0,
  findings_open: 2, rules_enabled: 19, rules_disabled: 0, config_source: "defaults", analyzer_versions: {}, warnings: [], analyzer_errors: [],
  error_message: null, ...over,
});
const status = (over: Record<string, unknown> = {}) => ({
  body: { state: "completed", latest: run(), findings_open: 2, index_generation: 1, analyzed_index_generation: 1, index_changed_since_run: false,
    config: { valid: true, source: "defaults", errors: [] }, message: "The latest run finished without warnings.", ...over },
});
const summary = (over: Record<string, unknown> = {}) => ({
  body: { analyzed: true, latest_run: run(), total_findings: 3, open: 2, by_severity: { high: 1, low: 1 }, by_category: { security: 1, maintainability: 1 },
    by_status: { new: 2, resolved: 1 }, resolved_by_reason: { fixed: 1 }, top_rules: [], files_analyzed: 9, analyzer_errors: 0, warnings: 0,
    notes: ["No composite quality score is computed."], ...over },
});
const finding = (over: Record<string, unknown> = {}) => ({
  id: "f1", repository_id: "r1", rule_id: "PY-SEC-001", analyzer_id: "py-security", rel_path: "src/pay.py", file_id: "file1", language: "Python",
  start_line: 12, end_line: 12, start_col: 4, end_col: 20, symbol_stable_id: null, scope: "charge", severity: "high", category: "security",
  title: "Use of eval or exec", explanation: "`charge` calls eval on a runtime value.", evidence: "Call to builtin eval; argument is not a literal.",
  remediation: "Use ast.literal_eval or a parser.", limitations: "Cannot tell whether the input is trusted.", confidence: "medium",
  metric_value: null, fingerprint: "a".repeat(40), status: "new", resolution_reason: null, suppression_reason: null, reopened_count: 0,
  first_seen_at: "2026-10-18T10:00:00Z", last_seen_at: "2026-10-18T10:00:00Z", resolved_at: null, analyzer_version: "1.0", rule_version: "1.0",
  first_run_id: "run1", last_run_id: "run1", ...over,
});
const page = (items: unknown[]) => ({ body: { items, total: items.length, page: 1, page_size: 20, pages: 1 } });
const base = { "GET /ready": readyOk, [`GET ${BASE}`]: { body: repoDetail } };

describe("code health workspace", () => {
  it("shows real summary numbers and the findings list", async () => {
    mockApi({ ...base, [`GET ${BASE}/analysis/status`]: status(), [`GET ${BASE}/analysis/summary`]: summary(),
      [`GET ${BASE}/findings`]: page([finding(), finding({ id: "f2", rule_id: "PY-MNT-005", title: "Unused import", severity: "low", rel_path: "src/a.py" })]) });
    renderApp(`${BASE}/health`);
    const overview = await screen.findByRole("region", { name: "Code health overview" });
    expect(within(overview).getByText("Open findings")).toBeInTheDocument();
    expect(within(overview).getByText("No composite quality score is computed.")).toBeInTheDocument();
    expect(await screen.findByText("Use of eval or exec")).toBeInTheDocument();
    expect(screen.getByText("Unused import")).toBeInTheDocument();
  });

  it("shows an empty state before the first analysis", async () => {
    mockApi({ ...base, [`GET ${BASE}/analysis/status`]: status({ state: "never_analyzed", latest: null, findings_open: 0, message: "This repository has not been analysed." }),
      [`GET ${BASE}/analysis/summary`]: summary({ analyzed: false, latest_run: null, open: 0, total_findings: 0, by_status: {} }),
      [`GET ${BASE}/findings`]: page([]) });
    renderApp(`${BASE}/health`);
    expect(await screen.findByText("Not analysed yet")).toBeInTheDocument();
    expect(await screen.findByText("No findings match")).toBeInTheDocument();
  });

  it("shows an explicit error with retry when findings fail to load", { timeout: 12_000 }, async () => {
    mockApi({ ...base, [`GET ${BASE}/analysis/status`]: status(), [`GET ${BASE}/analysis/summary`]: summary(),
      [`GET ${BASE}/findings`]: { status: 500, body: { error: { code: "internal_error", message: "An internal error occurred." } } } });
    renderApp(`${BASE}/health`);
    expect((await screen.findAllByRole("alert", {}, { timeout: 8000 }))[0]).toHaveTextContent("An internal error occurred.");
    expect(screen.getAllByRole("button", { name: "Try again" }).length).toBeGreaterThan(0);
  });

  it("asks the visitor to sign in when the server rejects the request for a missing or expired token", { timeout: 12_000 }, async () => {
    mockApi({ ...base, [`GET ${BASE}/analysis/status`]: status(), [`GET ${BASE}/analysis/summary`]: summary(),
      [`GET ${BASE}/findings`]: { status: 401, body: { error: { code: "unauthorized", message: "A valid bearer token is required." } } } });
    renderApp(`${BASE}/health`);
    expect(await screen.findByRole("heading", { name: "Sign in to NOVA" }, { timeout: 8000 })).toBeInTheDocument();
    expect(screen.getByLabelText("Access key")).toHaveAttribute("type", "password");
  });

  it("shows warnings, rule errors and invalid configuration, and blocks running when the config is invalid", async () => {
    mockApi({ ...base, [`GET ${BASE}/analysis/summary`]: summary({ analyzer_errors: 1, warnings: 1 }), [`GET ${BASE}/findings`]: page([]),
      [`GET ${BASE}/analysis/status`]: status({ state: "completed_with_warnings", config: { valid: false, source: "repository", errors: ["rules.PY-X: unknown rule id"] },
        latest: run({ status: "completed_with_warnings", warnings: ["1 file(s) could not be parsed"], analyzer_errors: [{ analyzer_id: "a", rule_id: "PY-COR-003", error_type: "RuntimeError", files: 2, first_file: "x.py" }] }) }) });
    renderApp(`${BASE}/health`);
    expect(await screen.findByText("rules.PY-X: unknown rule id")).toBeInTheDocument();
    expect(screen.getByText("1 file(s) could not be parsed")).toBeInTheDocument();
    expect(screen.getByText(/PY-COR-003 \(2 files\)/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run analysis" })).toBeDisabled();
  });

  it("prevents duplicate submissions while a run is starting", async () => {
    let posts = 0;
    mockApi({ ...base, [`GET ${BASE}/analysis/status`]: status(), [`GET ${BASE}/analysis/summary`]: summary(), [`GET ${BASE}/findings`]: page([]),
      [`POST ${BASE}/analysis`]: () => { posts += 1; return { status: 202, body: run({ status: "running" }) }; } });
    renderApp(`${BASE}/health`);
    const btn = await screen.findByRole("button", { name: "Run analysis" });
    fireEvent.click(btn); // two clicks dispatched before the first request can settle
    fireEvent.click(btn);
    await waitFor(() => expect(posts).toBe(1));
    await new Promise((r) => setTimeout(r, 50));
    expect(posts).toBe(1);
  });

  it("requires a reason and shows the server-confirmed status after suppressing", async () => {
    let current = finding();
    const patches: unknown[] = [];
    mockApi({ ...base, [`GET ${BASE}/analysis/status`]: status(), [`GET ${BASE}/analysis/summary`]: summary(),
      [`GET ${BASE}/findings`]: () => ({ body: { items: [current], total: 1, page: 1, page_size: 20, pages: 1 } }),
      [`PATCH ${BASE}/findings/f1`]: (init) => { patches.push(JSON.parse(String(init?.body))); current = finding({ status: "suppressed", suppression_reason: "Reviewed by team" }); return { body: current }; } });
    renderApp(`${BASE}/health`);
    await userEvent.click(await screen.findByRole("button", { name: /Use of eval or exec/ }));
    const detail = await screen.findByRole("complementary", { name: "Finding details" });
    expect(within(detail).getByText("Cannot tell whether the input is trusted.")).toBeInTheDocument();
    const submit = within(detail).getByRole("button", { name: "Suppress finding" });
    expect(submit).toBeDisabled();
    await userEvent.type(within(detail).getByLabelText(/Reason for suppressing/), "Reviewed by team");
    await userEvent.click(submit);
    expect(await within(detail).findByText("Finding suppressed.")).toBeInTheDocument();
    expect(patches).toEqual([{ status: "suppressed", reason: "Reviewed by team" }]);
    expect(await within(detail).findByRole("button", { name: "Remove suppression" })).toBeInTheDocument();
  });

  it("shows the server's error when a status update is rejected", async () => {
    mockApi({ ...base, [`GET ${BASE}/analysis/status`]: status(), [`GET ${BASE}/analysis/summary`]: summary(), [`GET ${BASE}/findings`]: page([finding()]),
      [`PATCH ${BASE}/findings/f1`]: { status: 409, body: { error: { code: "conflict", message: "A resolved finding cannot be suppressed." } } } });
    renderApp(`${BASE}/health`);
    await userEvent.click(await screen.findByRole("button", { name: /Use of eval or exec/ }));
    await userEvent.type(await screen.findByLabelText(/Reason for suppressing/), "valid reason");
    await userEvent.click(screen.getByRole("button", { name: "Suppress finding" }));
    expect(await screen.findByText("A resolved finding cannot be suppressed.")).toBeInTheDocument();
  });

  it("sends filters to the server", async () => {
    const urls: string[] = [];
    mockApi({ ...base, [`GET ${BASE}/analysis/status`]: status(), [`GET ${BASE}/analysis/summary`]: summary(),
      [`GET ${BASE}/findings`]: (_i, url) => { urls.push(url.search); return { body: { items: [finding()], total: 1, page: 1, page_size: 20, pages: 1 } }; } });
    renderApp(`${BASE}/health`);
    await screen.findByText("Use of eval or exec");
    await userEvent.selectOptions(screen.getByLabelText("Severity"), "high");
    await waitFor(() => expect(urls.some((u) => u.includes("severity=high"))).toBe(true));
  });
});
