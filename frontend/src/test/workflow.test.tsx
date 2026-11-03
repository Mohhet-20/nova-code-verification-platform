import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { repoDetail } from "./fixtures";
import { mockApi, readyOk, renderApp } from "./helpers";

afterEach(() => vi.unstubAllGlobals());

const BASE = "/repositories/r1";
const W = `${BASE}/workflow`;
const C = `${BASE}/ci`;
const HEAD = "4ff7dc8cf9895178aaaaaaaaaaaaaaaaaaaaaaaa";
const page = (items: unknown[]) => ({ body: { items, total: items.length, page: 1, page_size: 20, pages: 1 } });
const conn = (over = {}) => ({
  id: "k1", provider: "github", external_repo: "acme/shop", default_branch: "main", enabled: true, enabled_events: ["pull_request"], status: "unconfigured", status_context: "nova/verification",
  token_env: "NOVA_GH_TOKEN", token_configured: false, webhook_secret_env: "NOVA_GH_SECRET", webhook_secret_configured: true,
  capabilities: { webhooks: true, status_reporting: true, file_listing: true, local_branches: false }, last_validated_at: null, last_sync_at: null, last_error: "", created_at: "2026-10-19T10:00:00Z", updated_at: "2026-10-19T10:00:00Z", ...over,
});
const overview = (over = {}) => ({ connections: [conn()], change_requests: { open: 1, closed: 0, merged: 0 }, readiness_policy_version: 0, readiness_policy_is_default: true, can_admin: true, ...over });
const ver = (over = {}) => ({ latest_run_id: null, latest_head_sha: null, current_revision_verified: false, stale: false, gate_outcome: null, readiness: null, ...over });
const cr = (over = {}) => ({
  id: "cr1", provider: "github", external_id: "7", source_branch: "feature", target_branch: "main", head_sha: HEAD, base_sha: "b".repeat(40), title: "Add noop", author: "dev", state: "open", files_count: 1,
  files_source: "provider", files_truncated: false, head_changed_at: null, updated_at: "2026-10-19T10:00:00Z", verification: ver(), notes: [], changed_files: [{ path: "app_pkg/util.py", status: "modified" }], ...over,
});
const wf = (over = {}) => ({
  id: "w1", change_request_id: "cr1", head_sha: HEAD, base_sha: null, mode: "focused", trigger: "manual", status: "completed", cv_run_id: "run1", gate_outcome: "PASS", readiness: "READY", readiness_effective: "READY", stale: false,
  current_head_sha: HEAD, readiness_policy_version: 0, evaluated_at: "2026-10-19T10:01:00Z", error: null, requested_by: "u", created_at: "2026-10-19T10:00:00Z", finished_at: "2026-10-19T10:01:00Z", ...over,
});
const decision = (outcome = "READY") => ({
  workflow_run_id: "w1", outcome, stale: false,
  decision: { outcome, reason: outcome === "READY" ? "Every required rule is satisfied." : "A required rule failed.", revision: HEAD, meaning: "Decision support, not a guarantee of correctness.", precedence: ["NOT_READY", "BLOCKED", "INCOMPLETE", "READY"],
    rules: [{ id: "quality_gate", status: outcome === "READY" ? "pass" : "fail", required: true, detail: "gate detail" }] },
});
const report = (over = {}) => ({
  schema: "nova.ci.report/1", kind: "review", repository: { id: "r1", name: "demo" }, revision: { head_sha: HEAD, base_sha: null, evidence_head_sha: HEAD, evidence_is_current: true },
  change_request: { id: "cr1", provider: "github", external_id: "7", source_branch: "feature", target_branch: "main", title: "Add noop", author: "dev", state: "open", files_source: "provider" },
  changed_files: [], checks: [{ check_id: "tests", required: true, status: "passed", detail: "3 tests executed" }], quality_gate: { outcome: "PASS", reason: "ok" }, findings: [],
  security_analysis: { performed: false, note: "No dedicated security analyzers have run in this workflow. Absence of findings above is not a security conclusion." },
  regressions: { available: false, counts: null, items: [] }, release_readiness: null, gaps: [], limitations: ["Readiness and gate results are decision support, not proof of correctness."], ...over,
});
const base = (o = {}) => ({
  "GET /ready": readyOk, [`GET ${BASE}`]: { body: repoDetail }, [`GET ${C}/overview`]: { body: overview() }, [`GET ${C}/change-requests`]: page([cr()]), ...o,
});
const crRoutes = (o = {}) => ({
  ...base(), [`GET ${C}/change-requests/cr1`]: { body: cr() }, [`GET ${C}/change-requests/cr1/workflow-runs`]: { body: [] }, [`GET ${C}/change-requests/cr1/review-report`]: { body: report() }, ...o,
});

describe("Engineering Workflow Center", () => {
  it("shows connections without exposing secrets and says unconfigured plainly", async () => {
    mockApi(base());
    renderApp(W);
    expect(await screen.findByText("Engineering Workflow Center")).toBeInTheDocument();
    const row = await screen.findByTestId("conn-k1");
    expect(within(row).getByText("unconfigured")).toBeInTheDocument();
    expect(within(row).getByText("No API token")).toBeInTheDocument();
    expect(within(row).getByText("Webhook secret present")).toBeInTheDocument();
    expect(row).toHaveTextContent("NOVA_GH_TOKEN");
  });

  it("lists change requests with exact verification state and never claims ready without a decision", async () => {
    mockApi(base({ [`GET ${C}/change-requests`]: page([cr(), cr({ id: "cr2", title: "Stale one", verification: ver({ stale: true, readiness: "READY", latest_run_id: "w0" }) })]) }));
    renderApp(W);
    const list = await screen.findByRole("list", { name: "Change requests" });
    const first = within(list).getByTestId("cr-cr1");
    expect(within(first).getByText("Not verified")).toBeInTheDocument();
    expect(within(first).getByText("No decision")).toBeInTheDocument();
    expect(within(first).queryByText("READY")).not.toBeInTheDocument();
    const stale = within(list).getByTestId("cr-cr2");
    expect(within(stale).getByText("Stale")).toBeInTheDocument();
  });

  it("saves a GitHub connection with environment variable names only", async () => {
    const m = mockApi({ ...base(), [`POST ${C}/connections`]: { status: 201, body: conn() } });
    renderApp(W);
    await userEvent.selectOptions(await screen.findByLabelText("Provider"), "github");
    await userEvent.type(await screen.findByLabelText("GitHub repository (owner/name)"), "acme/shop");
    await userEvent.click(screen.getByRole("button", { name: "Save connection" }));
    await waitFor(() => expect(m.calls.some((c) => c.key === `POST ${C}/connections`)).toBe(true));
    const body = m.calls.find((c) => c.key === `POST ${C}/connections`)!.body as Record<string, string>;
    expect(body.token_env).toBe("NOVA_GITHUB_TOKEN");
    expect(body.external_repo).toBe("acme/shop");
  });

  it("hides connection management from non-owners", async () => {
    mockApi(base({ [`GET ${C}/overview`]: { body: overview({ can_admin: false }) } }));
    renderApp(W);
    expect(await screen.findByText(/Only a workspace owner can change provider connections/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Validate" })).not.toBeInTheDocument();
  });

  it("shows validation errors from a connection check", async () => {
    mockApi({ ...base(), [`POST ${C}/connections/k1/validate`]: { body: conn({ status: "error", last_error: "auth_failed: The provider answered HTTP 401." }) } });
    renderApp(W);
    await userEvent.click(await screen.findByRole("button", { name: "Validate" }));
    await waitFor(() => expect(screen.queryByText("Validating…")).not.toBeInTheDocument());
  });
});

describe("Change request page", () => {
  it("requires explicit confirmation before starting verification", async () => {
    const m = mockApi({ ...crRoutes(), [`POST ${C}/change-requests/cr1/workflow-runs`]: { status: 202, body: wf() } });
    renderApp(`${W}/change-requests/cr1`);
    await userEvent.click(await screen.findByRole("button", { name: "Start verification" }));
    expect(m.calls.some((c) => c.key === `POST ${C}/change-requests/cr1/workflow-runs`)).toBe(false);
    expect(screen.getByText(/never executed automatically/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Run verification" }));
    await waitFor(() => expect(m.calls.some((c) => c.key === `POST ${C}/change-requests/cr1/workflow-runs` && (c.body as { confirm: boolean }).confirm === true)).toBe(true));
  });

  it("warns about a stale revision and shows readiness as BLOCKED, never READY", async () => {
    mockApi(crRoutes({
      [`GET ${C}/change-requests/cr1`]: { body: cr({ verification: ver({ stale: true, latest_run_id: "w1", readiness: "READY" }) }) },
      [`GET ${C}/change-requests/cr1/workflow-runs`]: { body: [wf({ stale: true, readiness_effective: "BLOCKED", head_sha: "1".repeat(40) })] },
      [`GET ${C}/workflow-runs/w1/readiness`]: { body: { ...decision("BLOCKED"), stale: true } },
      [`GET ${C}/workflow-runs/w1/status-reports`]: { body: [] },
    }));
    renderApp(`${W}/change-requests/cr1`);
    expect(await screen.findByText(/newer commit landed after the last verification/)).toBeInTheDocument();
    const r = await screen.findByTestId("readiness");
    expect(within(r).getByText("BLOCKED")).toBeInTheDocument();
    expect(within(r).queryByText("READY")).not.toBeInTheDocument();
  });

  it("shows READY with its rules for the current revision and the status report outcome", async () => {
    mockApi(crRoutes({
      [`GET ${C}/change-requests/cr1`]: { body: cr({ verification: ver({ current_revision_verified: true, latest_run_id: "w1", readiness: "READY", gate_outcome: "PASS" }) }) },
      [`GET ${C}/change-requests/cr1/workflow-runs`]: { body: [wf()] },
      [`GET ${C}/workflow-runs/w1/readiness`]: { body: decision("READY") },
      [`GET ${C}/workflow-runs/w1/status-reports`]: { body: [{ id: "s1", workflow_run_id: "w1", provider: "github", head_sha: HEAD, context: "nova/verification", state: "success", description: "d", status: "unconfigured", attempts: 0, error_class: null, error: "Reporting intent recorded; nothing was sent.", retryable: false, next_retry_at: null, created_at: "2026-10-19T10:01:00Z" }] },
    }));
    renderApp(`${W}/change-requests/cr1`);
    const r = await screen.findByTestId("readiness");
    expect(within(r).getByText("READY")).toBeInTheDocument();
    expect(await screen.findByRole("list", { name: "Readiness rules" })).toHaveTextContent("quality_gate");
    const reps = await screen.findByRole("list", { name: "Status reports" });
    expect(within(reps).getByText("unconfigured")).toBeInTheDocument();
    expect(reps).toHaveTextContent("nothing was sent");
  });

  it("states that no security analysis was performed in the unified report", async () => {
    mockApi(crRoutes());
    renderApp(`${W}/change-requests/cr1`);
    const rep = await screen.findByTestId("review-report");
    expect(rep).toHaveTextContent("not a security conclusion");
    expect(rep).toHaveTextContent("No new static-analysis findings");
  });

  it("generates a report and offers JSON and Markdown downloads", async () => {
    mockApi(crRoutes({
      [`POST ${C}/change-requests/cr1/reports`]: { status: 201, body: { id: "rp1", kind: "review", change_request_id: "cr1", workflow_run_id: null, head_sha: HEAD, sha256: "ab".repeat(32), size_bytes: 1200, truncated: false, created_at: "2026-10-19T10:00:00Z" } },
      [`GET ${C}/reports/rp1`]: { body: { id: "rp1", kind: "review", change_request_id: "cr1", workflow_run_id: null, head_sha: HEAD, sha256: "ab".repeat(32), size_bytes: 1200, truncated: false, created_at: "2026-10-19T10:00:00Z", body: report(), markdown: "# NOVA pull-request review report" } },
    }));
    renderApp(`${W}/change-requests/cr1`);
    await userEvent.click(await screen.findByRole("button", { name: "Generate review report" }));
    const out = await screen.findByTestId("generated-report");
    expect(out).toHaveTextContent("NOVA pull-request review report");
    expect(within(out).getByRole("button", { name: "Download JSON" })).toBeInTheDocument();
    expect(within(out).getByRole("button", { name: "Download Markdown" })).toBeInTheDocument();
  });

  it("shows API errors when verification cannot start", async () => {
    mockApi({ ...crRoutes(), [`POST ${C}/change-requests/cr1/workflow-runs`]: { status: 409, body: { error: { code: "verification_in_progress", message: "A verification run is already active." } } } });
    renderApp(`${W}/change-requests/cr1`);
    await userEvent.click(await screen.findByRole("button", { name: "Start verification" }));
    await userEvent.click(screen.getByRole("button", { name: "Run verification" }));
    expect(await screen.findByText(/already active/)).toBeInTheDocument();
  });

  it("disables starting verification for a closed change request", async () => {
    mockApi(crRoutes({ [`GET ${C}/change-requests/cr1`]: { body: cr({ state: "closed" }) } }));
    renderApp(`${W}/change-requests/cr1`);
    expect(await screen.findByRole("button", { name: "Start verification" })).toBeDisabled();
  });
});
