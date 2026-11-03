import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, readyOk, renderApp } from "./helpers";
import { repoDetail } from "./fixtures";

afterEach(() => vi.unstubAllGlobals());

const BASE = "/repositories/r1";
const page = (items: unknown[]) => ({ body: { items, total: items.length, page: 1, page_size: 20, pages: 1 } });
const review = (over = {}) => ({
  id: "v1", mode: "working_tree", status: "completed", base_ref: null, head_ref: null, base_sha: null, head_sha: null, git_head: "abc", selected_paths: [],
  index_snapshot_id: "s1", index_stale: false, index_notes: [], files_total: 2, files_analyzed: 2, files_omitted: 0, lines_added: 5, lines_removed: 1,
  symbols_changed: 1, findings_total: 1, risk_state: "ranked", risk_score: 15.7, risk_category: "low", warnings: [], error_message: null,
  generator_version: "1.0", created_at: "2026-10-19T10:00:00Z", started_at: "2026-10-19T10:00:00Z", finished_at: "2026-10-19T10:00:05Z", ...over,
});
const summary = (over = {}, sumOver = {}) => ({
  review: review(over), ready: true, stages: [{ name: "acquire_changes", status: "ok" }, { name: "static_analysis", status: "ok" }],
  summary: { headline: "2 changed file(s) analysed; 1 finding(s).", by_severity: { high: 1 }, clean_review_note: null,
    ai: { used: false, note: "No AI provider was used." }, ...sumOver },
});
const finding = (over = {}) => ({
  id: "f1", stable_id: "abcd", fingerprint: "fp", review_id: "v1", rule_id: "PY-SEC-001", rel_path: "pkg/calc.py", start_line: 3, end_line: 3,
  severity: "high", category: "security", title: "Use of eval()", explanation: "eval executes arbitrary code.", evidence: "line 3 calls eval", remediation: "Use ast.literal_eval.",
  limitations: "Cannot see where the input comes from.", confidence: "high", actionable: true, source: "analyzer", ai_assisted: false, status: "open", rationale: null,
  inherited_from_review_id: null, related: {}, ...over,
});
const base = { "GET /ready": readyOk, [`GET ${BASE}`]: { body: repoDetail } };
const URL = `${BASE}/reviews/v1`;

describe("review creation", () => {
  it("explains what will be analysed and previews without starting", async () => {
    mockApi({
      ...base, [`GET ${BASE}/reviews`]: page([]),
      [`POST ${BASE}/reviews/preview`]: { body: { executes: false, mode: "working_tree", total_files: 1, omitted: 0, warnings: [], limits: { max_files: 200, max_diff_bytes: 1 },
        explanation: "Nothing is executed.", files: [{ rel_path: "pkg/calc.py", old_path: null, change_type: "modified", added: 1, removed: 1, binary: false, language: "python", is_test: false }] } },
    });
    renderApp(`${BASE}/reviews`);
    expect(await screen.findByText("No reviews yet")).toBeInTheDocument();
    expect(screen.getByText(/does not run tests or your code/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Preview what will be analysed/ }));
    expect(await screen.findByText("1 changed file(s) will be analysed.")).toBeInTheDocument();
    expect(screen.getByText(/modified · pkg\/calc\.py/)).toBeInTheDocument();
  });

  it("requires a base revision for revision comparisons and shows server validation errors", async () => {
    mockApi({ ...base, [`GET ${BASE}/reviews`]: page([]), [`POST ${BASE}/reviews`]: { status: 422, body: { error: { code: "validation_failed", message: "Unknown revision 'nope'." } } } });
    renderApp(`${BASE}/reviews`);
    await screen.findByText("No reviews yet");
    await userEvent.selectOptions(screen.getByLabelText("What to review"), "revisions");
    expect(screen.getByRole("button", { name: "Start review" })).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Base revision/), "nope");
    await userEvent.click(screen.getByRole("button", { name: "Start review" }));
    expect(await screen.findByText(/Unknown revision/)).toBeInTheDocument();
  });

  it("lists previous reviews with status and risk", async () => {
    mockApi({ ...base, [`GET ${BASE}/reviews`]: page([review(), review({ id: "v2", status: "failed", risk_category: null })]) });
    renderApp(`${BASE}/reviews`);
    expect(await screen.findByText("low change risk")).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
  });

  it("shows an error state with retry when listing fails", async () => {
    mockApi({ ...base, [`GET ${BASE}/reviews`]: { status: 404, body: { error: { code: "not_found", message: "Repository not found." } } } });
    renderApp(`${BASE}/reviews`);
    expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});

describe("review detail", () => {
  it("shows loading, then the summary with stages and the AI disclosure", async () => {
    mockApi({ ...base, [`GET ${URL}/summary`]: { body: summary() } });
    renderApp(URL);
    expect(await screen.findByText("Loading review…")).toBeInTheDocument();
    expect(await screen.findByText("2 changed file(s) analysed; 1 finding(s).")).toBeInTheDocument();
    expect(screen.getByText("No AI provider was used.")).toBeInTheDocument();
    expect(screen.getByText("static_analysis")).toBeInTheDocument();
  });

  it("makes partial failure explicit and never presents it as full success", async () => {
    mockApi({ ...base, [`GET ${URL}/summary`]: { body: { ...summary({ status: "completed_with_warnings", warnings: ["The static_analysis stage failed."], index_stale: true }),
      stages: [{ name: "static_analysis", status: "failed" }] } } });
    renderApp(URL);
    expect(await screen.findByText(/Partial results/)).toBeInTheDocument();
    expect(screen.getByText("The static_analysis stage failed.")).toBeInTheDocument();
    expect(screen.getByText("Completed with warnings")).toBeInTheDocument();
    expect(screen.getByText("Index stale or missing")).toBeInTheDocument();
  });

  it("shows a failed review's safe message and an error state when the review cannot be loaded", async () => {
    mockApi({ ...base, [`GET ${URL}/summary`]: { body: summary({ status: "failed", error_message: "Change acquisition failed." }) } });
    renderApp(URL);
    expect(await screen.findByText("Change acquisition failed.")).toBeInTheDocument();
  });

  it("shows an error state when the review is missing", async () => {
    mockApi({ ...base, [`GET ${URL}/summary`]: { status: 404, body: { error: { code: "not_found", message: "Review not found." } } } });
    renderApp(URL);
    expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("filters findings and confirms a dismissal from the server, requiring a rationale", async () => {
    let current = finding();
    const seen: string[] = [];
    mockApi({
      ...base, [`GET ${URL}/summary`]: { body: summary() },
      [`GET ${URL}/findings`]: () => { seen.push("list"); return { body: { items: [current], total: 1, page: 1, page_size: 20, pages: 1 } }; },
      [`PATCH ${URL}/findings/f1`]: (req?: RequestInit) => {
        const b = JSON.parse(String(req?.body ?? "{}"));
        if (b.status === "dismissed" && !(b.rationale ?? "").trim()) return { status: 422, body: { error: { code: "validation_failed", message: "A rationale of at least 5 characters is required to dismiss a finding." } } };
        current = finding({ status: b.status, rationale: b.rationale ?? null });
        return { body: current };
      },
    });
    renderApp(URL);
    await userEvent.click(await screen.findByRole("tab", { name: "Findings" }));
    expect(await screen.findByText("Use of eval()")).toBeInTheDocument();
    expect(screen.getByText("static analysis")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(await screen.findByText(/rationale of at least 5 characters/)).toBeInTheDocument();
    expect(screen.getAllByText("open").length).toBeGreaterThan(0);
    await userEvent.type(screen.getByLabelText(/Rationale for/), "Input is trusted config");
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(screen.getByText(/Rationale: Input is trusted config/)).toBeInTheDocument());
    expect(screen.getAllByText("dismissed").length).toBeGreaterThan(0);
  });

  it("shows a filter-specific empty state and an honest clean-review empty state", async () => {
    mockApi({ ...base, [`GET ${URL}/summary`]: { body: summary({ findings_total: 0 }) }, [`GET ${URL}/findings`]: page([]) });
    renderApp(URL);
    await userEvent.click(await screen.findByRole("tab", { name: "Findings" }));
    expect(await screen.findByText("No findings")).toBeInTheDocument();
    expect(screen.getByText(/does not show that the change is free of defects/)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Severity"), "critical");
    expect(await screen.findByText("No findings match these filters")).toBeInTheDocument();
  });

  it("explains risk by factor, with measured vs unavailable evidence and a disclaimer", async () => {
    mockApi({ ...base, [`GET ${URL}/summary`]: { body: summary() }, [`GET ${URL}/risk`]: { body: {
      ready: true, status: "completed", state: "ranked", score: 15.7, category: "low", formula: "100 * sum(weight * normalized) / sum(weight)",
      disclaimer: "A change-risk score is not a probability of defects.", thresholds: {}, evidence_share: 0.8, notes: [],
      factors: [
        { id: "change_size", label: "Change size", status: "measured", weight: 10, raw: 6, normalized: 0.2, contribution: 2, detail: "6 lines changed", evidence: [] },
        { id: "coverage", label: "Coverage of changed code", status: "unavailable", weight: 10, raw: null, normalized: null, contribution: null, detail: "No coverage data", evidence: [] },
      ] } } });
    renderApp(URL);
    await userEvent.click(await screen.findByRole("tab", { name: "Risk" }));
    expect(await screen.findByText("Score 15.7 / 100")).toBeInTheDocument();
    expect(screen.getByText(/not a probability of defects/)).toBeInTheDocument();
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText("No coverage data")).toBeInTheDocument();
  });

  it("does not show a number when risk is unknown", async () => {
    mockApi({ ...base, [`GET ${URL}/summary`]: { body: summary() }, [`GET ${URL}/risk`]: { body: {
      ready: true, status: "completed", state: "unknown", score: null, category: null, reason: "No analysable non-test code changed.", factors: [],
      formula: "f", disclaimer: "d", thresholds: {}, evidence_share: null, notes: [] } } });
    renderApp(URL);
    await userEvent.click(await screen.findByRole("tab", { name: "Risk" }));
    expect(await screen.findByText(/Risk could not be ranked: No analysable non-test code changed/)).toBeInTheDocument();
    expect(screen.queryByText(/Score/)).not.toBeInTheDocument();
  });

  it("discloses stale impact data and wording for affected tests", async () => {
    mockApi({ ...base, [`GET ${URL}/summary`]: { body: summary() }, [`GET ${URL}/impact`]: { body: {
      ready: true, index_stale: true, index_notes: ["pkg/calc.py changed after indexing"], graph_available: true, complete: false, limitations: ["Dependents beyond depth 2 are not listed."],
      max_depth: 2, dependents_total: 1, dependents_incomplete: true, dependents: [{ rel_path: "pkg/consumer.py", depth: 1, relationship: "confirmed_import" }],
      affected_tests: [{ rel_path: "tests/test_calc.py", relationship: "imports_changed_file", note: "Imports the changed file; this does not show it exercises the change." }] } } });
    renderApp(URL);
    await userEvent.click(await screen.findByRole("tab", { name: "Impact" }));
    expect(await screen.findByText(/may be incomplete/)).toBeInTheDocument();
    expect(screen.getByText(/pkg\/calc\.py changed after indexing/)).toBeInTheDocument();
    expect(screen.getByText(/does not show it exercises the change/)).toBeInTheDocument();
  });

  it("shows test recommendations as recommendations only", async () => {
    mockApi({ ...base, [`GET ${URL}/summary`]: { body: summary() }, [`GET ${URL}/test-recommendations`]: { body: { ready: true, total: 1, note: "No test was generated or executed.",
      items: [{ target: { rel_path: "pkg/calc.py", symbol: "percent" }, behavior: "Check existing callers still work.", reason: "Signature changed.", evidence: "e", evidence_source: "signature_change",
        priority: "high", confidence: "medium", status: "recommended_only", note: "Recommended only", existing_tests: { status: "imported_by_tests", tests: ["tests/test_calc.py"] } }] } } });
    renderApp(URL);
    await userEvent.click(await screen.findByRole("tab", { name: "Test recommendations" }));
    expect(await screen.findByText("No test was generated or executed.")).toBeInTheDocument();
    expect(screen.getByText("high priority")).toBeInTheDocument();
    expect(screen.getByText(/Existing tests: imported by tests/)).toBeInTheDocument();
  });

  it("lists changed files with symbols and flags stale-index mapping", async () => {
    mockApi({ ...base, [`GET ${URL}/summary`]: { body: summary() }, [`GET ${URL}/changes`]: page([{
      id: "c1", rel_path: "pkg/calc.py", change_type: "modified", old_path: null, new_path: "pkg/calc.py", language: "python", is_test: false, is_binary: false,
      lines_added: 1, lines_removed: 1, new_ranges: [[2, 2]], extraction_status: "ok", notes: [],
      symbols: [{ qualified_name: "helper", symbol_stable_id: null, kind: "function", start_line: 1, end_line: 2, change_kind: "modified", is_public: true,
        signature_old: "helper(x)", signature_new: "helper(x, y)", mapping: "fresh_parse", complexity: 1 }] }]) });
    renderApp(URL);
    await userEvent.click(await screen.findByRole("tab", { name: "Changes" }));
    expect(await screen.findByText("pkg/calc.py")).toBeInTheDocument();
    expect(screen.getByText(/not linked to the \(stale\) index/)).toBeInTheDocument();
    expect(screen.getByText(/helper\(x\) → helper\(x, y\)/)).toBeInTheDocument();
  });
});
