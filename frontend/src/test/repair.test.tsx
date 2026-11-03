import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, readyOk, renderApp } from "./helpers";
import { repoDetail } from "./fixtures";

afterEach(() => vi.unstubAllGlobals());

const BASE = "/repositories/r1";
const R = `${BASE}/repairs/i1`;
const page = (items: unknown[]) => ({ body: { items, total: items.length, page: 1, page_size: 20, pages: 1 } });
const item = (over = {}) => ({
  id: "i1", title: "Carts share state", description: "Two carts leak items.", severity: "high", status: "analyzed", source_kind: "failure", failure_id: "f1", finding_id: null,
  review_finding_id: null, debug_investigation_id: null, hint_files: [], relevant_files: ["pkg/cart.py"],
  root_cause: { state: "ranked", kind: "hypothesis", rel_path: "pkg/cart.py", symbol: "pkg.cart.add_item", confidence: "high", summary: "Most likely location: pkg/cart.py", confidence_rationale: "2 evidence items", note: "A hypothesis ranked from evidence, not a confirmed cause." },
  verification_outcome: null, evidence_assembled_at: "2026-10-19T10:00:00Z", proposal_generated_at: "2026-10-19T10:00:00Z", created_at: "2026-10-19T10:00:00Z",
  updated_at: "2026-10-19T10:00:00Z", closed_at: null, patch_count: 0, latest_patch_status: null, ...over,
});
const detail = (over = {}, ev = {}, prop = {}) => ({
  ...item(over), baseline_execution_run_id: null, index_snapshot_id: "s1",
  evidence: { state: "ranked", facts: [{ id: "F1", text: "Requester's description: leaks", source: "user_description", ref: "description", basis: "stated_by_requester" },
    { id: "F2", text: "Test failed: test_carts", source: "failure", ref: "f1", basis: "observed" }],
    hypotheses: [{ rank: 1, rel_path: "pkg/cart.py", symbol: "pkg.cart.add_item", start_line: 1, end_line: 4, score: 6.75, confidence: "high", kind: "hypothesis", status: "unverified",
      rationale: "pkg/cart.py: 2 evidence item(s)", evidence: [{ id: "loc:1", type: "localization", weight: 3.75, detail: "Failure localization rank 1." }], contradicting: [] }],
    unknowns: ["No measured coverage exists."], next_steps: ["Read pkg/cart.py lines 1-4."], limitations: ["Scores rank where to look first."], weights: { user_hint: 1 }, min_score: 2, ...ev },
  proposal: { state: "ready", strategy: "Default the parameter to None.", expected_behavior: "Each call gets a new list.", patch_available: true, patch_reason: "Verified deterministic fix.",
    side_effects: ["Callers relying on shared state change."], tests_to_run: [{ target: "tests/test_cart.py::test_carts", reason: "The original failing test.", kind: "failing_test" }], ...prop },
});
const patch = (over = {}) => ({
  id: "p1", investigation_id: "i1", source: "generated", rule_id: "PY-COR-002", title: "Fix mutable default", summary: "Deterministic fix.", status: "validated", patch_sha256: "a".repeat(64),
  files: [{ path: "pkg/cart.py", change_type: "modify", added: 1, removed: 1 }], extra_files: [], validation: { ok: true, errors: [], warnings: [], stale: false, static_check: { available: true, new_findings: [], resolved_findings: 1 } },
  validated_at: "x", submitted_at: null, approved_by: null, approved_at: null, rejected_at: null, reject_reason: null, applied_at: null, apply_result: {}, verification_summary: {},
  created_at: "2026-10-19T10:00:00Z", ...over,
});
const pdetail = (over = {}) => ({
  ...patch(over), diff_text: "x", preview: { files: [{ path: "pkg/cart.py", change_type: "modify", added: 1, removed: 1, hunks: [{ header: "@@ -1 +1 @@", lines: [
    { kind: "remove", text: "items=[]" }, { kind: "add", text: "<script>alert(1)</script>" }] }] }] },
});
const verification = (over = {}) => ({
  id: "v1", patch_id: "p1", investigation_id: "i1", execution_run_id: "e1", selected_tests: [{ target: "tests/test_cart.py::test_carts", reason: "The original failing test.", kind: "failing_test" }],
  outcome: "PASSED", outcome_reason: "All selected tests passed.", comparison: { baseline_available: true, new_failures: [], pre_existing_failures: ["tests.test_legacy::test_old"], no_longer_failing: ["tests.test_cart::test_carts"] },
  static_check: { available: true, new_findings: [], resolved_findings: 1 }, criteria: [{ id: "run_completed", status: "pass", detail: "The run finished with status 'passed'." }],
  created_at: "2026-10-19T10:00:00Z", evaluated_at: "2026-10-19T10:00:05Z", meaning: "Selected tests passed after the patch.",
  run: { status: "passed", exit_code: 0, command: [], started_at: null, finished_at: null, duration_ms: 219, tests_passed: 1, tests_failed: 0, output_truncated: false, stdout_tail: ".", stderr_tail: "", reason: null }, ...over,
});
const base = { "GET /ready": readyOk, [`GET ${BASE}`]: { body: repoDetail }, [`GET ${BASE}/failures`]: page([]) };
const audit = { body: { items: [{ id: "a1", action: "repair.created", created_at: "2026-10-19T10:00:00Z", details: {} }], total: 1, page: 1, page_size: 50, pages: 1 } };
const detailRoutes = (over = {}) => ({ ...base, [`GET ${R}`]: { body: detail() }, [`GET ${R}/audit`]: audit, [`GET ${R}/patches`]: { body: [] }, ...over });

describe("repair list", () => {
  it("shows an empty state and sends filters to the API", async () => {
    const m = mockApi({ ...base, [`GET ${BASE}/repairs`]: page([]) });
    renderApp(`${BASE}/repairs`);
    expect(await screen.findByText("No investigations yet")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Filter by status"), "applied");
    await waitFor(() => expect(m.fn.mock.calls.some((c) => String(c[0]).includes("status=applied"))).toBe(true));
    expect(await screen.findByText("No investigations match these filters")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Search investigations"), "cart");
    await waitFor(() => expect(m.fn.mock.calls.some((c) => String(c[0]).includes("q=cart"))).toBe(true));
  });

  it("lists investigations with distinct applied/verified/failed states", async () => {
    mockApi({ ...base, [`GET ${BASE}/repairs`]: page([item({ status: "applied", id: "a" , title: "A"}), item({ id: "b", title: "B", status: "verified", verification_outcome: "PASSED" }),
      item({ id: "c", title: "C", status: "verification_failed", verification_outcome: "FAILED" })]) });
    renderApp(`${BASE}/repairs`);
    await screen.findByRole("link", { name: "A" });
    const rows = screen.getAllByRole("listitem") as [HTMLElement, HTMLElement, HTMLElement];
    expect(within(rows[0]).getByText("Applied, NOT verified")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Verified")).toBeInTheDocument();
    expect(within(rows[1]).getByText("verification PASSED")).toBeInTheDocument();
    expect(within(rows[2]).getByText("Verification failed")).toBeInTheDocument();
    expect(within(rows[2]).getByText("verification FAILED")).toBeInTheDocument();
  });

  it("creates an investigation and surfaces server validation errors", async () => {
    mockApi({ ...base, [`GET ${BASE}/repairs`]: page([]), [`POST ${BASE}/repairs`]: { status: 422, body: { error: { code: "validation_failed", message: "Bad file hint." } } } });
    renderApp(`${BASE}/repairs`);
    await screen.findByText("No investigations yet");
    expect(screen.getByRole("button", { name: "Create investigation" })).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Title"), "Bug");
    await userEvent.click(screen.getByRole("button", { name: "Create investigation" }));
    expect(await screen.findByText("Bad file hint.")).toBeInTheDocument();
  });
});

describe("repair detail", () => {
  it("separates facts, hypotheses, unknowns and next steps, and labels the root cause unconfirmed", async () => {
    mockApi(detailRoutes());
    renderApp(R);
    const rc = await screen.findByTestId("root-cause");
    expect(within(rc).getByText("unconfirmed")).toBeInTheDocument();
    expect(within(rc).getByText(/not a confirmed cause/)).toBeInTheDocument();
    const facts = screen.getByRole("region", { name: "Facts" });
    expect(within(facts).getByText("stated by requester")).toBeInTheDocument();
    expect(within(facts).getByText("observed")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Hypotheses" })).getByText(/pkg\/cart\.py · pkg\.cart\.add_item/)).toBeInTheDocument();
    expect(screen.getByText("No measured coverage exists.")).toBeInTheDocument();
    expect(screen.getByText("Read pkg/cart.py lines 1-4.")).toBeInTheDocument();
    expect(screen.getByText("deterministic, no AI")).toBeInTheDocument();
  });

  it("states insufficient evidence and offers no suggested patch", async () => {
    mockApi(detailRoutes({ [`GET ${R}`]: { body: detail({ root_cause: { state: "insufficient_evidence", kind: "unknown" } }, { state: "insufficient_evidence", hypotheses: [] },
      { state: "insufficient_evidence", patch_available: false, patch_reason: "No evidence." }) } }));
    renderApp(R);
    expect(await screen.findByText(/Insufficient evidence/)).toBeInTheDocument();
    expect(screen.getByText(/No safe automatic patch is available/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create suggested patch" })).toBeDisabled();
  });

  it("reviews a patch: escaped diff, approval requires confirmation and does not apply", async () => {
    const m = mockApi(detailRoutes({
      [`GET ${R}/patches`]: { body: [patch({ status: "awaiting_approval" })] }, [`GET ${R}/patches/p1`]: { body: pdetail({ status: "awaiting_approval" }) },
      [`GET ${R}/patches/p1/verifications`]: { body: [] }, [`POST ${R}/patches/p1/approve`]: { body: pdetail({ status: "approved" }) },
    }));
    renderApp(R);
    const diff = await screen.findByTestId("diff-view");
    expect(within(diff).getByText("+<script>alert(1)</script>")).toBeInTheDocument();   // rendered as text, never as markup
    expect(diff.querySelector("script")).toBeNull();
    expect(screen.getByText(/Validated in a temporary copy/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve…" }));
    expect(m.calls.some((c) => c.key.endsWith("/approve"))).toBe(false);
    const dlg = screen.getByRole("alertdialog", { name: "Approve this patch?" });
    expect(dlg.textContent).toMatch(/does not change any file/);
    await userEvent.click(within(dlg).getByRole("button", { name: "Approve patch" }));
    await waitFor(() => expect(m.calls.some((c) => c.key === `POST ${R}/patches/p1/approve`)).toBe(true));
    expect(m.calls.some((c) => c.key.endsWith("/apply"))).toBe(false);
  });

  it("requires a reason to reject and can cancel an apply confirmation", async () => {
    const m = mockApi(detailRoutes({
      [`GET ${R}/patches`]: { body: [patch({ status: "approved" })] }, [`GET ${R}/patches/p1`]: { body: pdetail({ status: "approved" }) },
      [`GET ${R}/patches/p1/verifications`]: { body: [] },
    }));
    renderApp(R);
    await userEvent.click(await screen.findByRole("button", { name: "Reject…" }));
    expect(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Reject patch" })).toBeDisabled();
    await userEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Cancel" }));
    await userEvent.click(screen.getByRole("button", { name: "Apply to working tree…" }));
    const dlg = screen.getByRole("alertdialog", { name: "Apply this patch to your working tree?" });
    expect(within(dlg).getByText(/Nothing is committed/)).toBeInTheDocument();
    await userEvent.click(within(dlg).getByRole("button", { name: "Cancel" }));
    expect(m.calls.some((c) => c.key.endsWith("/apply") || c.key.endsWith("/reject"))).toBe(false);
  });

  it("shows stale/invalid warnings and a role error for a viewer", async () => {
    mockApi(detailRoutes({
      [`GET ${R}/patches`]: { body: [patch({ status: "invalid", validation: { ok: false, stale: true, errors: [{ code: "stale", message: "pkg/cart.py changed since the patch was created." }] } })] },
      [`GET ${R}/patches/p1`]: { body: pdetail({ status: "invalid", validation: { ok: false, stale: true, errors: [{ code: "stale", message: "pkg/cart.py changed since the patch was created." }] } }) },
      [`GET ${R}/patches/p1/verifications`]: { body: [] },
      [`POST ${R}/patches/p1/validate`]: { status: 403, body: { error: { code: "forbidden", message: "no" } } },
    }));
    renderApp(R);
    expect(await screen.findByText(/Stale: a file changed/)).toBeInTheDocument();
    expect(screen.getByText(/no longer valid for the current files/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Validate" }));
    expect(await screen.findByText(/Your role does not permit this action/)).toBeInTheDocument();
  });

  it("shows applied-not-verified, then verification history with rationale, criteria and baseline", async () => {
    mockApi(detailRoutes({
      [`GET ${R}`]: { body: detail({ status: "applied" }) },
      [`GET ${R}/patches`]: { body: [patch({ status: "applied", applied_at: "2026-10-19T10:01:00Z" })] },
      [`GET ${R}/patches/p1`]: { body: pdetail({ status: "applied", applied_at: "2026-10-19T10:01:00Z" }) },
      [`GET ${R}/patches/p1/rollback`]: { body: { originals_retained: true, applied: true, reverse_diff: "--- a/x", instructions: ["NOVA did not commit anything."], current_matches_patched_result: true } },
      [`GET ${R}/patches/p1/verifications`]: { body: [verification(), verification({ id: "v2", outcome: "BLOCKED", outcome_reason: "No tests discovered.", run: null, comparison: { baseline_available: false, note: "No baseline." } })] },
    }));
    renderApp(R);
    expect(await screen.findByText("Applied but not yet verified.")).toBeInTheDocument();
    expect(await screen.findByText(/This does not mean it works: run verification/)).toBeInTheDocument();
    const cards = (await screen.findAllByTestId("verification")) as [HTMLElement, HTMLElement];
    expect(within(cards[0]).getByText("PASSED")).toBeInTheDocument();
    expect(within(cards[0]).getByText(/test_old/)).toBeInTheDocument();
    expect(within(cards[0]).getByText("The original failing test.", { exact: false })).toBeInTheDocument();
    expect(within(cards[0]).getByText(/exit 0/)).toBeInTheDocument();
    expect(within(cards[1]).getByText("BLOCKED")).toBeInTheDocument();
    expect(within(cards[1]).getByText("No baseline.")).toBeInTheDocument();
    expect(await screen.findByText("Recovery instructions")).toBeInTheDocument();
  });

  it("asks for confirmation with the selected tests before running verification", async () => {
    const m = mockApi(detailRoutes({
      [`GET ${R}/patches`]: { body: [patch({ status: "applied" })] }, [`GET ${R}/patches/p1`]: { body: pdetail({ status: "applied" }) },
      [`GET ${R}/patches/p1/rollback`]: { body: { originals_retained: true, applied: true, reverse_diff: "", instructions: [], current_matches_patched_result: true } },
      [`GET ${R}/patches/p1/verifications`]: { body: [] },
      [`POST ${R}/patches/p1/verify/preview`]: { body: { executes: false, requires: "confirm", patch_status: "applied", selected: [{ target: "tests/test_cart.py::test_carts", reason: "The original failing test.", kind: "failing_test" }], notes: [], execution: "x" } },
      [`POST ${R}/patches/p1/verifications`]: { status: 202, body: verification({ outcome: "PENDING" }) },
    }));
    renderApp(R);
    await userEvent.click(await screen.findByRole("button", { name: "Verify with tests…" }));
    const dlg = await screen.findByRole("alertdialog", { name: "Run tests to verify this patch?" });
    expect(await within(dlg).findByText(/The original failing test/)).toBeInTheDocument();
    expect(m.calls.some((c) => c.key === `POST ${R}/patches/p1/verifications`)).toBe(false);
    await userEvent.click(within(dlg).getByRole("button", { name: "Run verification" }));
    await waitFor(() => expect(m.calls.find((c) => c.key === `POST ${R}/patches/p1/verifications`)?.body).toMatchObject({ confirm: true }));
  });
});

describe("AI-assisted proposals", () => {
  const ws = (ai_provider: string) => ({ body: { workspace_id: "w", workspace_name: "W", user_handle: "u", user_display_name: "U", role: "owner",
    auth_mode: "local-trust", multi_user_isolation: false, ai_provider, planner: "deterministic-baseline", version: "0.19.0", environment: "development",
    limits: {}, registration_restricted: false } });

  it("is hidden unless the server enables Claude", async () => {
    mockApi(detailRoutes({ "GET /workspace": ws("none") }));
    renderApp(R);
    expect(await screen.findByRole("button", { name: "Supply my own patch" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Propose a fix with Claude" })).toBeNull();
  });

  it("requests a Claude proposal, explains what is sent, and labels the draft as needing review", async () => {
    let created = false;
    const aiPatch = patch({ source: "ai_assisted", title: "AI-assisted fix: Carts share state", status: "draft",
      summary: "AI-assisted proposal (claude-opus-5-5, confidence high). Not trusted: it must pass validation, human approval and test verification." });
    const m = mockApi(detailRoutes({
      "GET /workspace": ws("anthropic"),
      [`GET ${R}/patches`]: () => ({ body: created ? [aiPatch] : [] }),
      [`POST ${R}/patches`]: () => { created = true; return { status: 201, body: pdetail({ source: "ai_assisted", status: "draft" }) }; },
      [`GET ${R}/patches/p1`]: { body: pdetail({ source: "ai_assisted", status: "draft" }) }, [`GET ${R}/patches/p1/verifications`]: { body: [] },
    }));
    renderApp(R);
    const button = await screen.findByRole("button", { name: "Propose a fix with Claude" });
    expect(screen.getByText(/Claude sees this investigation, its evidence and its relevant files \(secrets scrubbed\)/)).toBeInTheDocument();
    await userEvent.click(button);
    await waitFor(() => expect(m.calls.find((c) => c.key === `POST ${R}/patches`)?.body).toEqual({ source: "ai_assisted" }));
    expect(await screen.findByText("AI-assisted · human review required")).toBeInTheDocument();
    expect(screen.getByText(/Proposed by Claude, checked by NOVA/)).toBeInTheDocument();
  });

  it("shows the server's reason when Claude's edits are rejected", async () => {
    mockApi(detailRoutes({
      "GET /workspace": ws("anthropic"),
      [`POST ${R}/patches`]: { status: 422, body: { error: { code: "ai_edit_rejected", message: "Claude's proposal was rejected by NOVA's checks; nothing was changed." } } },
    }));
    renderApp(R);
    await userEvent.click(await screen.findByRole("button", { name: "Propose a fix with Claude" }));
    expect(await screen.findByText(/rejected by NOVA's checks; nothing was changed/)).toBeInTheDocument();
  });
});
