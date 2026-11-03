import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, readyOk, renderApp } from "./helpers";
import { repoDetail } from "./fixtures";

afterEach(() => vi.unstubAllGlobals());

const BASE = "/repositories/r1";
const RF = `${BASE}/refactors`;
const T = `${RF}/t1`;
const page = (items: unknown[]) => ({ body: { items, total: items.length, page: 1, page_size: 20, pages: 1 } });
const fresh = (over = {}) => ({ body: { state: "fresh", snapshot_id: "s1", generation: 1, indexed_at: null, last_attempt_status: "completed", changed: 0, new: 0, removed: 0, sample_paths: [], git_head_changed: null, note: "", ...over } });
const type = (over = {}) => ({
  id: "python.rename_symbol", title: "Rename a module-level Python symbol", support: "applicable", mode: "transform", languages: ["python"], inputs: [], preconditions: ["File is indexed"], scope: "Module scope plus importers",
  semantic_requirement: "Scope analysis", edit_types: [], validation: [], verification: [], limitations: ["Dynamic access is reported, not edited"], reason: "", category: "rename", ...over,
});
const catalog = { body: { types: [type(), type({ id: "analysis.duplicates", title: "Find duplicated code", support: "proposal_only", mode: "analysis" }),
  type({ id: "python.rename_module", title: "Rename or move a module", support: "unsupported", reason: "Cannot be proven complete.", limitations: [], preconditions: [] })],
  support_levels: { applicable: "A diff can be applied.", proposal_only: "Suggestions only.", unsupported: "Rejected." } } };
const item = (over = {}) => ({
  id: "t1", repository_id: "r1", type_id: "python.rename_symbol", title: "Rename add_item to append_item (pkg/cart.py)", status: "generated", support_level: "applicable", index_snapshot_id: "s1",
  target: { symbol: "add_item", rel_path: "pkg/cart.py" }, patch_sha256: "a".repeat(64), files: [{ path: "pkg/cart.py", change_type: "modify", base_sha256: "b", result_sha256: "c", added: 2, removed: 2 }],
  verification_summary: {}, created_by: "u1", created_at: "2026-10-19T10:00:00Z", updated_at: "2026-10-19T10:00:00Z", validated_at: null, approved_at: null, applied_at: null, blockers: 0, warnings: 1, ...over,
});
const plan = (over = {}) => ({
  version: "1.0", id: "t1", type: "python.rename_symbol", support: "applicable", snapshot: { id: "s1", index_state: "fresh", complete: true, notes: [] }, target: { symbol: "add_item", rel_path: "pkg/cart.py" },
  files: [], edits: [{ id: "e1", rel_path: "pkg/cart.py", line: 4, col: 4, end_col: 12, old: "add_item", new: "append_item", kind: "definition", confidence: "certain" }], edit_count: 1,
  findings: [{ severity: "warning", code: "public_api", message: "add_item is exported.", locations: [] }, { severity: "info", code: "comment_mentions", message: "1 comment mentions it.", locations: [] }],
  conflicts: [], side_effects: [], limitations: ["Dynamic references are not edited."], acknowledgements_required: ["public_api"], candidate_edits: [], preconditions: [], postconditions: [],
  verification_strategy: { tests: "t", proves: "selected tests passed", does_not_prove: "behavioral equivalence" }, extra: {}, ...over,
});
const detail = (over = {}, p = {}) => ({
  ...item(over), params: {}, plan: plan(p), impact: { index_complete: true, direct_dependents: ["pkg/use.py"], dependent_summary: { direct: 1, transitive: 0, max_depth: 4, truncated: false, more_beyond_depth: false },
    cycles: { new_cycles: [], proof: "The set of imported modules is unchanged." }, affected_tests: { discovered: true, associated: { "pkg/cart.py": ["tests/test_cart.py"] }, basis: "static" } },
  validation: {}, apply_result: {}, reject_reason: null, submitted_at: null, approved_by: null, rejected_at: null, applied_by: null, baseline_execution_run_id: null, baseline_note: null, has_diff: true, ...over,
});
const diff = { body: { id: "t1", status: "generated", has_diff: true, patch_sha256: "a", preview: { files: [{ path: "pkg/cart.py", change_type: "modify", added: 1, removed: 1,
  hunks: [{ header: "@@ -1 +1 @@", lines: [{ kind: "remove", text: "def add_item" }, { kind: "add", text: "def <b>append_item</b>" }] }] }] } } };
const audit = { body: { items: [{ id: "a1", action: "refactor.created", created_at: "2026-10-19T10:00:00Z", details: {} }], total: 1, page: 1, page_size: 50, pages: 1 } };
const base = { "GET /ready": readyOk, [`GET ${BASE}`]: { body: repoDetail }, [`GET ${BASE}/freshness`]: fresh(), [`GET ${RF}/types`]: catalog };
const detailRoutes = (over = {}) => ({ ...base, [`GET ${T}`]: { body: detail() }, [`GET ${T}/diff`]: diff, [`GET ${T}/audit`]: audit, ...over });
const verification = (over = {}) => ({
  id: "v1", transformation_id: "t1", execution_run_id: "e1", baseline_execution_run_id: "b1", selected_tests: [], outcome: "FAILED", outcome_reason: "1 test(s) failed or errored.",
  meaning: "A check failed.", comparison: { baseline_available: true, new_failures: ["tests.test_dyn::test_dynamic"] }, static_check: {},
  criteria: [{ id: "no_failures", status: "fail", detail: "1 test(s) failed or errored." }, { id: "behavior_equivalence", status: "unavailable", detail: "Not proven." }],
  created_at: "2026-10-19T10:00:00Z", evaluated_at: "2026-10-19T10:00:05Z",
  run: { status: "failed", exit_code: 1, command: [], started_at: null, finished_at: null, duration_ms: 10, tests_passed: 1, tests_failed: 1, output_truncated: false, stdout_tail: "F", stderr_tail: "", reason: null }, ...over,
});

describe("refactoring workbench", () => {
  it("shows the catalog with support levels, explicit unsupported reasons, and an empty history", async () => {
    mockApi({ ...base, [`GET ${RF}`]: page([]) });
    renderApp(RF);
    const dup = await screen.findByTestId("type-analysis.duplicates");
    expect(within(dup).getByText("Proposal only")).toBeInTheDocument();
    const un = screen.getByTestId("type-python.rename_module");
    expect(within(un).getByText("Unsupported")).toBeInTheDocument();
    expect(within(un).getByText("Cannot be proven complete.")).toBeInTheDocument();
    expect(await screen.findByText("No refactorings yet")).toBeInTheDocument();
  });

  it("warns when the index is stale and when it was never built", async () => {
    mockApi({ ...base, [`GET ${BASE}/freshness`]: fresh({ state: "stale", changed: 1, new: 2, removed: 0 }), [`GET ${RF}`]: page([]) });
    renderApp(RF);
    expect(await screen.findByTestId("stale-index")).toHaveTextContent(/stale.*2 new/);
  });

  it("sends history filters to the API and shows the no-match state", async () => {
    const m = mockApi({ ...base, [`GET ${RF}`]: page([]) });
    renderApp(RF);
    await screen.findByText("No refactorings yet");
    await userEvent.selectOptions(screen.getByLabelText("Filter by status"), "applied");
    await waitFor(() => expect(m.fn.mock.calls.some((c) => String(c[0]).includes("status=applied"))).toBe(true));
    expect(await screen.findByText("No refactorings match these filters")).toBeInTheDocument();
  });

  it("lists states distinctly: applied is never shown as verified", async () => {
    mockApi({ ...base, [`GET ${RF}`]: page([item({ id: "a", status: "applied", title: "A" }), item({ id: "b", status: "verified", title: "B" }), item({ id: "c", status: "blocked", title: "C", blockers: 2 })]) });
    renderApp(RF);
    await screen.findByRole("link", { name: "A" });
    const rows = within(screen.getByRole("list", { name: "Refactorings" })).getAllByRole("listitem") as [HTMLElement, HTMLElement, HTMLElement];
    expect(within(rows[0]).getByText("Applied, NOT verified")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Verified by tests")).toBeInTheDocument();
    expect(within(rows[2]).getByText("Blocked")).toBeInTheDocument();
  });

  it("finds a symbol, fills the form and surfaces server errors", async () => {
    const hit = { id: "s1", stable_id: "x", name: "add_item", qualified_name: "pkg.cart.add_item", kind: "function", language: "Python", file_id: "f1", file_path: "pkg/cart.py", start_line: 4, start_col: 0, end_line: 7, end_col: 0, matched_on: "name" };
    const m = mockApi({ ...base, [`GET ${RF}`]: page([]), [`GET ${BASE}/symbols/search`]: { body: { items: [hit], total: 1, page: 1, page_size: 20, pages: 1, ambiguous: false, index: null } },
      [`POST ${RF}/rename`]: { status: 422, body: { error: { code: "unsupported_type", message: "Not supported." } } } });
    renderApp(RF);
    await screen.findByText("No refactorings yet");
    expect(screen.getByRole("button", { name: "Create plan" })).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Find a symbol"), "add_item");
    await userEvent.click(await screen.findByRole("button", { name: /pkg\.cart\.add_item/ }));
    expect(screen.getByLabelText("File")).toHaveValue("pkg/cart.py");
    await userEvent.type(screen.getByLabelText("New name"), "append_item");
    await userEvent.click(screen.getByRole("button", { name: "Create plan" }));
    expect(await screen.findByText("Not supported.")).toBeInTheDocument();
    expect(m.calls.find((c) => c.key === `POST ${RF}/rename`)?.body).toMatchObject({ type_id: "python.rename_symbol", rel_path: "pkg/cart.py", name: "add_item", new_name: "append_item" });
  });
});

describe("refactoring detail", () => {
  it("shows plan, findings, impact and an escaped multi-file diff", async () => {
    mockApi(detailRoutes());
    renderApp(T);
    const plan_ = await screen.findByTestId("plan");
    expect(within(plan_).getByText("public_api")).toBeInTheDocument();
    expect(within(plan_).getByText(/Dynamic references are not edited/)).toBeInTheDocument();
    expect(within(plan_).getByText(/behavioral equivalence/)).toBeInTheDocument();
    const impact = screen.getByTestId("impact");
    expect(within(impact).getByText(/The set of imported modules is unchanged/)).toBeInTheDocument();
    expect(within(impact).getByText(/tests\/test_cart\.py/)).toBeInTheDocument();
    const d = await screen.findByTestId("diff-view");
    expect(within(d).getByText("+def <b>append_item</b>")).toBeInTheDocument();
    expect(d.querySelector("b")).toBeNull();
  });

  it("warns when the plan was made against an incomplete index", async () => {
    mockApi(detailRoutes({ [`GET ${T}`]: { body: detail({}, { snapshot: { id: "s1", index_state: "stale", complete: false, notes: [] } }) } }));
    renderApp(T);
    expect(await screen.findByText(/incomplete or stale index/)).toBeInTheDocument();
  });

  it("requires acknowledging warnings before approval and does not apply", async () => {
    const m = mockApi(detailRoutes({ [`GET ${T}`]: { body: detail({ status: "awaiting_approval" }) }, [`POST ${T}/approve`]: { body: detail({ status: "approved" }) } }));
    renderApp(T);
    await userEvent.click(await screen.findByRole("button", { name: "Approve…" }));
    const dlg = screen.getByRole("alertdialog", { name: "Approve this refactoring?" });
    const ok = within(dlg).getByRole("button", { name: "Approve" });
    expect(ok).toBeDisabled();
    await userEvent.click(within(dlg).getByRole("checkbox"));
    expect(ok).toBeEnabled();
    await userEvent.click(ok);
    await waitFor(() => expect(m.calls.some((c) => c.key === `POST ${T}/approve`)).toBe(true));
    expect(m.calls.find((c) => c.key === `POST ${T}/approve`)?.body).toEqual({ acknowledged_warnings: ["public_api"] });
    expect(m.calls.some((c) => c.key.endsWith("/apply"))).toBe(false);
  });

  it("applies only after an explicit confirmation", async () => {
    const m = mockApi(detailRoutes({ [`GET ${T}`]: { body: detail({ status: "approved" }) }, [`POST ${T}/apply`]: { body: detail({ status: "applied", applied_at: "2026-10-19T11:00:00Z", apply_result: { ok: true } }) },
      [`GET ${T}/rollback`]: { body: { originals_retained: true, applied: true, reverse_diff: "", instructions: [], current_matches_refactored_result: true } }, [`GET ${T}/verifications`]: { body: [] } }));
    renderApp(T);
    await userEvent.click(await screen.findByRole("button", { name: "Apply to working tree…" }));
    expect(m.calls.some((c) => c.key.endsWith("/apply"))).toBe(false);
    const dlg = screen.getByRole("alertdialog", { name: "Apply to the working tree?" });
    expect(within(dlg).getByText(/Nothing is committed/)).toBeInTheDocument();
    await userEvent.click(within(dlg).getByRole("button", { name: "Apply changes" }));
    await waitFor(() => expect(m.calls.find((c) => c.key === `POST ${T}/apply`)?.body).toEqual({ confirm: true }));
  });

  it("shows a blocked request as non-applicable with no approve or apply actions", async () => {
    mockApi(detailRoutes({ [`GET ${T}`]: { body: detail({ status: "blocked", has_diff: false, blockers: 1 }, { findings: [{ severity: "blocker", code: "stale_index", message: "Files were added.", locations: [] }], candidate_edits: [] }) } }));
    renderApp(T);
    expect(await screen.findByText(/blocked and cannot be applied/)).toBeInTheDocument();
    expect(screen.getByText("Files were added.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve|Apply|Submit/ })).toBeNull();
  });

  it("an applied change is labelled not verified, and a failed verification is shown as FAILED with its criteria", async () => {
    mockApi(detailRoutes({ [`GET ${T}`]: { body: detail({ status: "verification_failed", applied_at: "2026-10-19T11:00:00Z", apply_result: { ok: true } }) },
      [`GET ${T}/rollback`]: { body: { originals_retained: true, applied: true, reverse_diff: "x", instructions: ["Undo it."], current_matches_refactored_result: true } },
      [`GET ${T}/verifications`]: { body: [verification()] } }));
    renderApp(T);
    const v = await screen.findByTestId("verification");
    expect(within(v).getByText("FAILED")).toBeInTheDocument();
    expect(within(v).getByText("no_failures")).toBeInTheDocument();
    expect(within(v).getByText("behavior_equivalence")).toBeInTheDocument();
    expect(screen.queryByText("Verified by tests")).toBeNull();
    expect(screen.getByText("Verification failed")).toBeInTheDocument();
  });

  it("a passing verification is described as evidence, not proof", async () => {
    mockApi(detailRoutes({ [`GET ${T}`]: { body: detail({ status: "verified", verification_summary: { outcome: "PASSED" }, applied_at: "x", apply_result: {} }) },
      [`GET ${T}/rollback`]: { body: { originals_retained: true, applied: true, reverse_diff: "", instructions: [], current_matches_refactored_result: true } },
      [`GET ${T}/verifications`]: { body: [verification({ outcome: "PASSED", outcome_reason: "Every criterion passed.", criteria: [{ id: "run_completed", status: "pass", detail: "ok" }], comparison: { baseline_available: false, note: "No baseline." } })] } }));
    renderApp(T);
    expect((await screen.findAllByText(/evidence, not proof/)).length).toBeGreaterThan(0);
    expect(await screen.findByText("No baseline.")).toBeInTheDocument();
  });

  it("starts verification only after reviewing the selected tests", async () => {
    const m = mockApi(detailRoutes({ [`GET ${T}`]: { body: detail({ status: "applied", applied_at: "x", apply_result: {} }) },
      [`GET ${T}/rollback`]: { body: { originals_retained: true, applied: true, reverse_diff: "", instructions: [], current_matches_refactored_result: true } }, [`GET ${T}/verifications`]: { body: [] },
      [`POST ${T}/verify/preview`]: { body: { executes: false, requires: "x", status: "applied", selected: [{ target: "tests/test_cart.py", reason: "imports changed file", kind: "imports_changed_file" }], notes: [],
        baseline_execution_run_id: null, baseline_note: "No test run before the change exists.", not_proven: "Not proven.", execution: "Restricted subprocess." } },
      [`POST ${T}/verifications`]: { body: verification({ outcome: "PENDING" }) } }));
    renderApp(T);
    expect(await screen.findByText(/Not verified yet/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Review and run verification" }));
    const dlg = await screen.findByRole("alertdialog", { name: "Run tests now?" });
    expect(within(dlg).getByText(/tests\/test_cart\.py/)).toBeInTheDocument();
    expect(m.calls.some((c) => c.key === `POST ${T}/verifications`)).toBe(false);
    await userEvent.click(within(dlg).getByRole("button", { name: "Run tests" }));
    await waitFor(() => expect(m.calls.find((c) => c.key === `POST ${T}/verifications`)?.body).toMatchObject({ confirm: true, scope: "selected" }));
  });

  it("renders duplicate groups with evidence, filters and an empty state", async () => {
    const group = { id: "g1", kind: "renamed", lines_total: 16, similarity: 1, average_similarity: 1, similarity_meaning: "1.0 = identical after normalising identifiers.", auto_merge: false, pairs: [],
      members: [{ rel_path: "pkg/a.py", name: "total_a", kind: "function", start_line: 1, end_line: 8, lines: 8, tokens: 60, excerpt: "def total_a(items):" }, { rel_path: "pkg/b.py", name: "total_b", kind: "function", start_line: 1, end_line: 8, lines: 8, tokens: 60, excerpt: "def total_b(rows):" }],
      shared: { token_count_range: [60, 60], shared_identifiers: [], shared_calls: [] }, differences: { behavioral: false, summary: "Only identifier spelling differs." },
      strategies: [{ id: "parameterize", summary: "Merge into one function.", caution: "c" }], limitations: [] };
    const m = mockApi(detailRoutes({ [`GET ${T}`]: { body: detail({ type_id: "analysis.duplicates", status: "analyzed", support_level: "proposal_only", has_diff: false, files: [] }, { extra: {} }) },
      [`GET ${T}/duplicates`]: (_i: RequestInit | undefined, url: URL) => ({ body: { id: "t1", total: url.searchParams.get("kind") === "exact" ? 0 : 1, page: 1, page_size: 10,
        groups: url.searchParams.get("kind") === "exact" ? [] : [group], stats: { files_scanned: 2, units_compared: 2 }, filters: {}, limitations: ["No automatic merge."], index: { complete: true } } }) }));
    renderApp(T);
    const g = await screen.findByTestId("dup-group");
    expect(within(g).getByText("renamed")).toBeInTheDocument();
    expect(within(g).getByText(/pkg\/b\.py:1-8/)).toBeInTheDocument();
    expect(within(g).getByText(/Only identifier spelling differs/)).toBeInTheDocument();
    expect(screen.getAllByText(/Analysis only/).length).toBeGreaterThan(0);
    await userEvent.selectOptions(screen.getByLabelText("Filter by kind"), "exact");
    await waitFor(() => expect(m.fn.mock.calls.some((c) => String(c[0]).includes("kind=exact"))).toBe(true));
    expect(await screen.findByText("No duplicate groups")).toBeInTheDocument();
  });

  it("shows an error state and a role notice for forbidden actions", async () => {
    mockApi({ ...base, [`GET ${T}`]: { status: 404, body: { error: { code: "transformation_not_found", message: "No such refactoring in this repository." } } } });
    renderApp(T);
    expect(await screen.findByText(/No such refactoring/)).toBeInTheDocument();
  });

  it("explains a forbidden validate without losing the page", async () => {
    mockApi(detailRoutes({ [`POST ${T}/validate`]: { status: 403, body: { error: { code: "forbidden", message: "Read-only." } } } }));
    renderApp(T);
    await userEvent.click(await screen.findByRole("button", { name: "Validate" }));
    expect(await screen.findByText(/Your role does not permit this action/)).toBeInTheDocument();
    expect(screen.getByTestId("plan")).toBeInTheDocument();
  });
});
