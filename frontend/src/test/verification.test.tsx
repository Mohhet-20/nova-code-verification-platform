import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, readyOk, renderApp } from "./helpers";
import { repoDetail } from "./fixtures";

afterEach(() => vi.unstubAllGlobals());

const BASE = "/repositories/r1";
const V = `${BASE}/verification`;
const page = (items: unknown[]) => ({ body: { items, total: items.length, page: 1, page_size: 20, pages: 1 } });
const rules = { default_mode: "focused", static: { enforce: true, max_new_findings: 0 }, tests: { min_tests_executed: 1, allow_preexisting_failures: false, max_skipped: null }, coverage: { min_line_percent: null, max_drop_percent: null }, baseline: { required: false }, require_unchanged_tree: true };
const baseline = (over = {}) => ({ id: "b1", label: "clean", status: "valid", run_id: "br", execution_run_id: "be", head_sha: "4ff7dc8cf9895178", tree_fingerprint: "f", scope: { kind: "all" }, failing: [], totals: { collected: 4, passed: 4, failed: 0 }, coverage: {}, integrity_ok: true, created_by: "u", created_at: "2026-10-19T10:00:00Z", ...over });
const summary = (over = {}) => ({
  id: "run1", kind: "verify", status: "completed", mode: "focused", source: "working_tree", head_sha: "4ff7dc8cf9895178", base_sha: null, tree_fingerprint: "abcdef0123456789", policy_version: 0, baseline_id: "b1",
  execution_run_id: "e1", gate_outcome: "PASS", reason: "Every required criterion has verified evidence and satisfies the policy.", cancel_requested: false, files_changed: 1, link: {}, requested_by: "u",
  created_at: "2026-10-19T10:00:00Z", started_at: "2026-10-19T10:00:01Z", finished_at: "2026-10-19T10:00:02Z", ...over,
});
const check = (over = {}) => ({ id: "c1", check_id: "tests", tier: 2, required: true, reason: "1 test file(s) selected by static import association.", confidence: "medium", prerequisites: ["test discovery"], status: "passed", result: { detail: "3 test(s) executed, none failed.", failures: [] }, started_at: null, finished_at: null, ...over });
const gate = (outcome = "PASS", over = {}) => ({ outcome, reason: outcome === "PASS" ? "Every required criterion has verified evidence." : "A required criterion failed.", precedence: ["FAIL", "BLOCKED", "INCOMPLETE", "PASS"], rules: [{ id: "check:tests", status: outcome === "PASS" ? "pass" : "fail", required: true, detail: "tests detail", evidence: {} }], meaning: "m", ...over });
const reg = (over = {}) => ({ available: true, items: [], counts: { new: 0, pre_existing: 0, flaky: 0, unclassified: 0, resolved: 0 }, basis: "A failure is 'new' only when a compatible baseline covered the test.", baseline: { compatible: true, problems: [], baseline_id: "b1" }, ...over });
const detail = (over = {}) => ({
  ...summary(), change_set: { source: "working_tree", base_sha: null, head_sha: null, git_head: "x", fingerprint: "f", warnings: [], files: [{ path: "app_pkg/util.py", status: "modified", old_path: null, sha256: "s", notes: [] }], counts: { total: 1, python: 1, docs: 0 } },
  plan: { mode: "focused", mode_meaning: "m", checks: [], test_targets: [], unverified_paths: [], notes: [], binding_problems: [], impact: {} }, checks: [check()], policy_snapshot: rules, gate: gate(), regression: reg(), coverage: null, static: null,
  freshness: { stale: false, reason: null }, ...over,
});
const overview = (over = {}) => ({ policy_version: 0, policy_is_default: true, policy: rules, latest_run: summary(), latest_baseline: baseline(), active_run: null, gate_counts: { PASS: 2, FAIL: 1, BLOCKED: 0, INCOMPLETE: 0 }, ...over });
const policy = (over = {}) => ({ body: { version: 0, is_default: true, rules, can_edit: true, precedence: ["FAIL", "BLOCKED", "INCOMPLETE", "PASS"], ...over } });
const base = (o = {}) => ({
  "GET /ready": readyOk, [`GET ${BASE}`]: { body: repoDetail }, [`GET ${V}/overview`]: { body: overview() }, [`GET ${V}/policy`]: policy(), [`GET ${V}/baselines`]: { body: [baseline()] },
  [`GET ${V}/runs`]: page([summary()]), ...o,
});
const runRoutes = (d: unknown, over = {}) => ({ ...base(), [`GET ${V}/runs/run1`]: { body: d }, [`GET ${V}/runs/run1/events`]: { body: [{ id: "e1", kind: "run_queued", detail: {}, created_at: "2026-10-19T10:00:00Z" }] }, ...over });

describe("Continuous Verification Center", () => {
  it("shows the overview with real counts, the latest decision and the baseline", async () => {
    mockApi(base());
    renderApp(V);
    expect(await screen.findByText("Continuous Verification Center")).toBeInTheDocument();
    expect(await screen.findByText(/Latest baseline:/)).toHaveTextContent("clean");
    const hist = await screen.findByRole("list", { name: "Verification runs" });
    expect(within(hist).getByText("PASS")).toBeInTheDocument();
    const stat = (k: string) => screen.getAllByText(k).find((e) => e.tagName === "DT")?.nextElementSibling?.textContent;
    expect([stat("PASS"), stat("FAIL"), stat("BLOCKED"), stat("INCOMPLETE")]).toEqual(["2", "1", "0", "0"]);
  });

  it("warns when no baseline exists and shows the empty history", async () => {
    mockApi(base({ [`GET ${V}/overview`]: { body: overview({ latest_baseline: null, latest_run: null }) }, [`GET ${V}/baselines`]: { body: [] }, [`GET ${V}/runs`]: page([]) }));
    renderApp(V);
    expect(await screen.findByText(/No baseline recorded/)).toBeInTheDocument();
    expect(await screen.findByText("No verification runs yet")).toBeInTheDocument();
    expect(await screen.findByText("No baselines")).toBeInTheDocument();
  });

  it("previews the plan without starting a run, then starts a run only on request", async () => {
    const preview = { executes: false, change_set: { source: "working_tree", base_sha: null, head_sha: null, git_head: "x", fingerprint: "f", warnings: [], files: [{ path: "app_pkg/util.py", status: "modified", old_path: null, sha256: "s", notes: [] }], counts: { total: 1, python: 1, docs: 0 } },
      plan: { mode: "focused", mode_meaning: "Tests that import the changed modules.", checks: [{ check_id: "tests", tier: 2, required: true, reason: "1 test file selected.", confidence: "medium", prerequisites: [], status: "planned" }], test_targets: ["tests/test_util.py"], unverified_paths: [], notes: [], binding_problems: [], impact: {} } };
    const m = mockApi({ ...base(), [`POST ${V}/changes/preview`]: { body: preview }, [`POST ${V}/runs`]: { status: 202, body: detail({ status: "queued", gate_outcome: null, gate: null, checks: [] }) }, [`GET ${V}/runs/run1`]: { body: detail() }, [`GET ${V}/runs/run1/events`]: { body: [] } });
    renderApp(V);
    await userEvent.click(await screen.findByRole("button", { name: /Preview plan/ }));
    const plan = await screen.findByTestId("plan-preview");
    expect(within(plan).getByText("app_pkg/util.py")).toBeInTheDocument();
    expect(within(plan).getByText("tests")).toBeInTheDocument();
    expect(m.calls.some((c) => c.key === `POST ${V}/runs`)).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "Run verification" }));
    await waitFor(() => expect(m.calls.some((c) => c.key === `POST ${V}/runs` && (c.body as { confirm: boolean }).confirm === true)).toBe(true));
    expect(await screen.findByText(/Verification run · focused/)).toBeInTheDocument();
  });

  it("reports an empty change set honestly", async () => {
    mockApi({ ...base(), [`POST ${V}/changes/preview`]: { body: { executes: false, change_set: { source: "working_tree", base_sha: null, head_sha: null, git_head: "x", fingerprint: "f", warnings: [], files: [], counts: { total: 0, python: 0, docs: 0 } }, plan: { mode: "focused", mode_meaning: "m", checks: [], test_targets: [], unverified_paths: [], notes: [], binding_problems: [], impact: {} } } } });
    renderApp(V);
    await userEvent.click(await screen.findByRole("button", { name: /Preview plan/ }));
    expect(await screen.findByText(/nothing to verify/)).toBeInTheDocument();
  });

  it("shows API errors from starting a run", async () => {
    mockApi({ ...base(), [`POST ${V}/runs`]: { status: 409, body: { error: { code: "run_in_progress", message: "A verification run is already active." } } } });
    renderApp(V);
    await userEvent.click(await screen.findByRole("button", { name: "Run verification" }));
    expect(await screen.findByText(/already active/)).toBeInTheDocument();
  });

  it("lets only an owner edit the policy and sends the new values", async () => {
    const m = mockApi({ ...base(), [`PUT ${V}/policy`]: policy({ version: 1, is_default: false }) });
    renderApp(V);
    const min = await screen.findByLabelText("Minimum tests executed");
    await userEvent.clear(min);
    await userEvent.type(min, "3");
    await userEvent.click(screen.getByRole("button", { name: "Save new version" }));
    await waitFor(() => expect(m.calls.some((c) => c.key === `PUT ${V}/policy`)).toBe(true));
    const put = m.calls.find((c) => c.key === `PUT ${V}/policy`)!.body as { rules: { tests: { min_tests_executed: number } } };
    expect(put.rules.tests.min_tests_executed).toBe(3);
  });

  it("makes the policy read-only for non-owners", async () => {
    mockApi(base({ [`GET ${V}/policy`]: policy({ can_edit: false }) }));
    renderApp(V);
    expect(await screen.findByLabelText("Minimum tests executed")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Save new version" })).not.toBeInTheDocument();
    expect(screen.getByText(/Only a workspace owner/)).toBeInTheDocument();
  });

  it("filters history by outcome", async () => {
    const m = mockApi(base());
    renderApp(V);
    await userEvent.selectOptions(await screen.findByLabelText("Outcome filter"), "fail");
    await waitFor(() => expect(m.fn.mock.calls.some((c) => String(c[0]).includes("outcome=fail"))).toBe(true));
  });
});

describe("verification run detail", () => {
  it("shows PASS only when the backend decided PASS, with each check", async () => {
    mockApi(runRoutes(detail()));
    renderApp(`${V}/run1`);
    expect(await screen.findByTestId("check-tests")).toHaveTextContent("3 test(s) executed");
    expect(screen.getAllByText("PASS").length).toBeGreaterThan(0);
    expect(screen.queryByTestId("stale-run")).not.toBeInTheDocument();
  });

  it.each(["FAIL", "BLOCKED", "INCOMPLETE"])("shows %s exactly and never PASS", async (o) => {
    mockApi(runRoutes(detail({ gate_outcome: o, gate: gate(o), reason: "Not a pass." })));
    renderApp(`${V}/run1`);
    await screen.findByTestId("check-tests");
    expect(screen.getAllByText(o).length).toBeGreaterThan(0);
    expect(screen.queryByText("PASS")).not.toBeInTheDocument();
  });

  it("shows no decision while running and refreshes by polling until finished", async () => {
    let n = 0;
    const m = mockApi({ ...runRoutes(detail()), [`GET ${V}/runs/run1`]: () => ({ body: ++n < 2 ? detail({ status: "running", gate_outcome: null, gate: null, checks: [check({ status: "running", result: {} })] }) : detail() }) });
    renderApp(`${V}/run1`);
    expect(await screen.findByText("No decision (running)")).toBeInTheDocument();
    expect(screen.queryByText("PASS")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel run" })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("No decision (running)")).not.toBeInTheDocument(), { timeout: 5000 });
    expect(screen.getAllByText("PASS").length).toBeGreaterThan(0);
    expect(m.calls.filter((c) => c.key === `GET ${V}/runs/run1`).length).toBeGreaterThanOrEqual(2);
  }, 10000);

  it("cancels an active run through the API", async () => {
    const m = mockApi({ ...runRoutes(detail({ status: "running", gate_outcome: null, gate: null })), [`POST ${V}/runs/run1/cancel`]: { body: detail({ status: "cancelled", gate_outcome: "INCOMPLETE" }) } });
    renderApp(`${V}/run1`);
    await userEvent.click(await screen.findByRole("button", { name: "Cancel run" }));
    await waitFor(() => expect(m.calls.some((c) => c.key === `POST ${V}/runs/run1/cancel`)).toBe(true));
  });

  it("marks an old result stale and says it describes the earlier state", async () => {
    mockApi(runRoutes(detail({ freshness: { stale: true, reason: "The working tree or HEAD changed after this run; its result describes an earlier state." } })));
    renderApp(`${V}/run1`);
    expect(await screen.findByTestId("stale-run")).toHaveTextContent(/earlier state/);
    expect(screen.getByText("Stale")).toBeInTheDocument();
  });

  it("lists new failures with their explanation and counts", async () => {
    const items = [{ test: "tests.test_util::test_clamp_low", category: "new", explanation: "The baseline covered this test and it did not fail there.", message: "assert 10 == 0", evidence: {} }];
    mockApi(runRoutes(detail({ gate_outcome: "FAIL", gate: gate("FAIL"), regression: reg({ items, counts: { new: 1, pre_existing: 0, flaky: 0, unclassified: 0, resolved: 0 } }) })));
    renderApp(`${V}/run1`);
    await userEvent.click(await screen.findByRole("tab", { name: "Regressions" }));
    const list = await screen.findByRole("list", { name: "Regression findings" });
    expect(within(list).getByText("tests.test_util::test_clamp_low")).toBeInTheDocument();
    expect(within(list).getByText("new")).toBeInTheDocument();
    expect(screen.getByLabelText("Regression counts")).toHaveTextContent("new: 1");
  });

  it("says classification is unavailable when no compatible baseline exists", async () => {
    mockApi(runRoutes(detail({ regression: { available: false, reason: "no compatible baseline" } })));
    renderApp(`${V}/run1`);
    await userEvent.click(await screen.findByRole("tab", { name: "Regressions" }));
    expect(await screen.findByText(/cannot be classified as new or pre-existing/)).toBeInTheDocument();
  });

  it("shows each gate criterion with its evidence and the precedence", async () => {
    mockApi(runRoutes(detail({ gate_outcome: "FAIL", gate: gate("FAIL") })));
    renderApp(`${V}/run1`);
    await userEvent.click(await screen.findByRole("tab", { name: "Quality gate" }));
    const t = await screen.findByRole("table", { name: "Gate criteria" });
    expect(within(t).getByText("check:tests")).toBeInTheDocument();
    expect(within(t).getByText("tests detail")).toBeInTheDocument();
    expect(screen.getByText(/FAIL > BLOCKED > INCOMPLETE > PASS/)).toBeInTheDocument();
  });

  it("says no decision exists instead of inventing one", async () => {
    mockApi(runRoutes(detail({ status: "queued", gate_outcome: null, gate: null, checks: [] })));
    renderApp(`${V}/run1`);
    await userEvent.click(await screen.findByRole("tab", { name: "Quality gate" }));
    expect(await screen.findByText(/No decision yet/)).toBeInTheDocument();
  });

  it("recomputes a baseline comparison on request and flags incompatibility", async () => {
    const cmp = { run_id: "run1", baseline_id: "b1", persisted: false, compatibility: { compatible: false, problems: ["Different test scope."] }, regression: reg({ available: false, reason: "incompatible" }), coverage: {}, note: "This comparison does not change the recorded gate decision." };
    mockApi(runRoutes(detail({ baseline_id: null }), { [`GET ${V}/runs/run1/compare`]: { body: cmp } }));
    renderApp(`${V}/run1`);
    await userEvent.click(await screen.findByRole("tab", { name: "Baseline comparison" }));
    await userEvent.selectOptions(await screen.findByLabelText("Baseline"), "b1");
    expect(await screen.findByText(/Not comparable: Different test scope/)).toBeInTheDocument();
    expect(screen.getByText(/does not change the recorded gate decision/)).toBeInTheDocument();
  });

  it("shows an error state when the run cannot be loaded", async () => {
    mockApi({ ...base(), [`GET ${V}/runs/run1`]: { status: 404, body: { error: { code: "not_found", message: "Verification run not found." } } } });
    renderApp(`${V}/run1`);
    expect(await screen.findByText(/Verification run not found/)).toBeInTheDocument();
  });
});
