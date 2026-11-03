import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, readyOk, renderApp } from "./helpers";
import { repoDetail } from "./fixtures";

afterEach(() => vi.unstubAllGlobals());

const BASE = "/repositories/r1";
const page = (items: unknown[]) => ({ body: { items, total: items.length, page: 1, page_size: 20, pages: 1 } });
const discovery = (over = {}) => ({
  id: "d1", status: "completed_with_warnings", framework: "pytest", framework_config: {}, started_at: "2026-10-18T10:00:00Z",
  finished_at: "2026-10-18T10:00:01Z", files_found: 2, files_unparsable: 1, tests_found: 1, source_files_with_tests: 1,
  source_files_total: 3, errors: ["tests/test_broken.py: syntax error"], excluded: [], ...over,
});
const overview = (over = {}) => ({
  body: { discovery: discovery(), note: "Discovered tests were found by static analysis only. Nothing was imported or executed.", generation_runs: 1,
    candidates_by_status: {}, execution_runs_by_status: {}, latest_coverage_run_id: null, ...over },
});
const neverCov = { body: { state: "never_measured", message: "No coverage has been measured.", run: null, unmeasured_targets: [], basis: null } };
const run = (over = {}) => ({
  id: "x1", kind: "generated", generation_run_id: "g1", status: "passed", reason: null, created_at: "2026-10-18T10:00:00Z",
  started_at: "2026-10-18T10:00:00Z", finished_at: "2026-10-18T10:00:01Z", duration_ms: 1000, exit_code: 0, command: [],
  environment_profile: {}, tests_collected: 4, tests_passed: 4, tests_failed: 0, tests_errors: 0, tests_skipped: 0, failures: [],
  stdout_tail: "4 passed", stderr_tail: "", output_truncated: false, coverage_status: "measured", coverage_error: null,
  coverage_tool: "coverage.py 7.16.2", coverage_branch: true, lines_total: 10, lines_covered: 7, line_percent: 70, branches_total: 6,
  branches_covered: 3, branch_percent: 50, tool_versions: {}, ...over,
});
const cand = (over = {}) => ({
  id: "c1", target_path: "pkg/calc.py", target_symbol: null, module: "pkg.calc", cases: [], confidence: "medium", review_required: true,
  runnable_count: 3, size_bytes: 100, content_sha256: "a", status: "validated", validation: [{ check: "syntax", passed: true, detail: "ok" }],
  rejection_reasons: [], warnings: [], approved_at: null, exported_path: null, source: "def test_a():\n    assert 1 < 2\n", ...over,
});
const gen = (over = {}) => ({
  id: "g1", status: "validated", targets: [{ path: "pkg/calc.py", symbol: null }], generator_version: "1.0", tool_versions: {},
  warnings: [], created_at: "2026-10-18T10:00:00Z", candidates: [cand()], created: true, ...over,
});
const base = { "GET /ready": readyOk, [`GET ${BASE}`]: { body: repoDetail } };

describe("tests & coverage workspace", () => {
  it("shows an honest empty state before anything is discovered or measured", async () => {
    mockApi({ ...base, [`GET ${BASE}/tests`]: overview({ discovery: null, note: "Tests have not been discovered yet." }), [`GET ${BASE}/coverage`]: neverCov });
    renderApp(`${BASE}/testing`);
    expect(await screen.findByText("No discovery yet")).toBeInTheDocument();
    expect(screen.getByText("Not measured")).toBeInTheDocument();
    expect(screen.queryByText(/0\.0%/)).not.toBeInTheDocument();
  });

  it("discovery tab lists files, parse errors, and says discovered is not passing", async () => {
    mockApi({ ...base, [`GET ${BASE}/tests`]: overview(), [`GET ${BASE}/coverage`]: neverCov,
      [`GET ${BASE}/tests/files`]: page([
        { id: "f1", rel_path: "tests/test_calc.py", framework: "pytest", parse_status: "ok", parse_error: null, test_count: 1, tests: [],
          associated_sources: [{ path: "pkg/calc.py", via: "import", confidence: "high" }], notes: [] },
        { id: "f2", rel_path: "tests/test_broken.py", framework: "pytest", parse_status: "syntax_error", parse_error: "invalid syntax", test_count: 0, tests: [], associated_sources: [], notes: [] }]) });
    renderApp(`${BASE}/testing`);
    await userEvent.click(await screen.findByRole("tab", { name: "Discovery" }));
    expect(await screen.findByText("tests/test_calc.py")).toBeInTheDocument();
    expect(screen.getByText("invalid syntax")).toBeInTheDocument();
    expect(screen.getByText(/Discovered does not mean executed or passing/)).toBeInTheDocument();
  });

  it("coverage tab labels static gaps as recommendations and requires confirmation to run the suite", async () => {
    const mock = mockApi({ ...base, [`GET ${BASE}/tests`]: overview(), [`GET ${BASE}/coverage`]: neverCov,
      [`GET ${BASE}/coverage/files`]: page([]),
      [`GET ${BASE}/coverage/gaps`]: { body: { items: [{ rel_path: "pkg/unused.py", symbol: "never_imported", kind: "function", start_line: 1, end_line: 2,
        complexity: 1, reasons: ["no discovered test imports this module"], has_associated_tests: false, associated_tests: [], measured: null,
        evidence: "static", confidence: "low", limitations: "Static recommendation: it shows likely untested behaviour, not proof of missing coverage." }],
        total: 1, page: 1, page_size: 10, coverage_run_id: null, discovery_id: "d1", note: "Items marked 'static' are recommendations." } },
      [`POST ${BASE}/tests/execute`]: { status: 202, body: run({ kind: "existing", status: "pending" }) } });
    renderApp(`${BASE}/testing`);
    await userEvent.click(await screen.findByRole("tab", { name: "Coverage" }));
    expect(await screen.findByText("Coverage has not been measured")).toBeInTheDocument();
    expect(await screen.findByText("Static recommendation")).toBeInTheDocument();
    const button = screen.getByRole("button", { name: /Run existing suite/ });
    expect(button).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox"));
    expect(button).toBeEnabled();
    await userEvent.click(button);
    await waitFor(() => expect(mock.calls.some((c) => c.key === `POST ${BASE}/tests/execute`)).toBe(true));
  });

  it("shows measured numbers with their basis when coverage exists", async () => {
    mockApi({ ...base, [`GET ${BASE}/tests`]: overview(),
      [`GET ${BASE}/coverage`]: { body: { state: "measured", message: "Measured by coverage.py.", run: run(), unmeasured_targets: [], basis: "generated tests only; this is not the coverage of the whole suite" } },
      [`GET ${BASE}/coverage/files`]: page([{ rel_path: "pkg/calc.py", statements: 10, covered: 7, line_percent: 70, missing_lines: [[15, 17]], branches: 6, branches_covered: 3 }]),
      [`GET ${BASE}/coverage/gaps`]: { body: { items: [], total: 0, page: 1, page_size: 10, coverage_run_id: "x1", discovery_id: null, note: "n" } } });
    renderApp(`${BASE}/testing`);
    await userEvent.click(await screen.findByRole("tab", { name: "Coverage" }));
    expect(await screen.findByText(/generated tests only/)).toBeInTheDocument();
    expect(screen.getByText("15-17")).toBeInTheDocument();
    expect(screen.getAllByText("70.0%").length).toBeGreaterThan(0);
  });

  it("review panel renders generated code as escaped text and gates execution on approval", async () => {
    const evil = "def test_a():\n    x = '<img src=x onerror=alert(1)>'\n";
    const mock = mockApi({ ...base, [`GET ${BASE}/tests`]: overview(),
      [`GET ${BASE}/tests/generations`]: page([gen()]),
      [`GET ${BASE}/tests/generations/g1`]: { body: gen({ candidates: [cand({ source: evil })] }) },
      [`POST ${BASE}/tests/generations/g1/approve`]: { body: gen({ status: "approved", candidates: [cand({ status: "approved", source: evil })] }) } });
    const { container } = renderApp(`${BASE}/testing`);
    await userEvent.click(await screen.findByRole("tab", { name: "Generate" }));
    await userEvent.click(await screen.findByRole("button", { name: "Review" }));
    const code = await screen.findByLabelText("Generated test code (read-only)");
    expect(code.textContent).toContain("<img src=x onerror=alert(1)>");
    expect(container.querySelector("img")).toBeNull();
    expect(code.getAttribute("contenteditable")).toBeNull();
    expect(screen.getByRole("button", { name: "Run approved tests" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Approve validated" }));
    await waitFor(() => expect(mock.calls.some((c) => c.key.endsWith("/approve"))).toBe(true));
    expect(mock.calls.some((c) => c.key.endsWith("/execute"))).toBe(false);
  });

  it("shows a permission message instead of a generic failure on 403", async () => {
    mockApi({ ...base, [`GET ${BASE}/tests`]: overview({ discovery: null }), [`GET ${BASE}/coverage`]: neverCov,
      [`POST ${BASE}/tests/discover`]: { status: 403, body: { error: { code: "forbidden", message: "Your role does not permit this action." } } } });
    renderApp(`${BASE}/testing`);
    fireEvent.click(await screen.findByRole("button", { name: "Discover tests" }));
    expect(await screen.findByText(/Your role does not permit this action\. You can still read results\./)).toBeInTheDocument();
  });

  it("runs tab shows failures, truncation and the not-a-sandbox notice; error state is shown on API failure", async () => {
    mockApi({ ...base, [`GET ${BASE}/tests`]: overview(),
      [`GET ${BASE}/tests/runs`]: page([run({ status: "failed", exit_code: 1, tests_passed: 3, tests_failed: 1, output_truncated: true,
        failures: [{ name: "tests/test_a.py::test_bad", message: "assert 1 == 2", traceback: "E assert 1 == 2" }], coverage_status: "not_requested", line_percent: null, branch_percent: null })]) });
    renderApp(`${BASE}/testing`);
    await userEvent.click(await screen.findByRole("tab", { name: "Runs" }));
    const details = await screen.findByRole("region", { name: "Run details" });
    expect(within(details).getByText("tests/test_a.py::test_bad")).toBeInTheDocument();
    expect(within(details).getByText(/Not a sandbox/)).toBeInTheDocument();
    expect(within(details).getByText(/truncated/)).toBeInTheDocument();
    expect(within(details).getByText(/not requested/)).toBeInTheDocument();
  });

  it("surfaces an error with retry when loading fails", async () => {
    mockApi({ ...base, [`GET ${BASE}/tests`]: { status: 404, body: { error: { code: "not_found", message: "boom" } } }, [`GET ${BASE}/coverage`]: neverCov });
    renderApp(`${BASE}/testing`);
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
  });
});
