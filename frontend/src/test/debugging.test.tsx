import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, readyOk, renderApp } from "./helpers";
import { repoDetail } from "./fixtures";

afterEach(() => vi.unstubAllGlobals());

const BASE = "/repositories/r1";
const page = (items: unknown[]) => ({ body: { items, total: items.length, page: 1, page_size: 20, pages: 1 } });
const failure = (over = {}) => ({
  id: "f1", category: "exception", exception_type: "ValueError", test_id: "tests/test_a.py::test_exc", summary: "ValueError: bad value 0",
  status: "open", source: "pytest_text", occurrence_count: 2, first_seen_at: "2026-10-18T10:00:00Z", last_seen_at: "2026-10-18T11:00:00Z",
  top_location: "pkg/calc.py:3", investigation_status: null, investigation_id: null, ...over,
});
const detail = (over = {}) => ({
  ...failure(), message: "ValueError: bad value 0 password=[REDACTED]", fingerprint: "abc", warnings: [], frames_truncated: false, revision: null,
  revision_known: false, index_snapshot_id: "s1", occurrences: [], related_findings: [], coverage: {}, freshness: {}, localization_meta: {},
  frames: [
    { idx: 0, display_path: "tests/test_a.py", rel_path: "tests/test_a.py", function: "test_exc", function_source: "trace", line: 5, col: null, scope: "repository", is_test: true, symbol_stable_id: null },
    { idx: 1, display_path: "pkg/calc.py", rel_path: "pkg/calc.py", function: "helper", function_source: "index", line: 3, col: null, scope: "repository", is_test: false, symbol_stable_id: "x" },
  ], ...over,
});
const loc = (over = {}) => ({
  failure_id: "f1", state: "ranked", analysis_version: "1.0", missing_evidence: ["Coverage was not measured."], notes: [], stale: false, limitations: [], weights: {},
  interpretation: "Scores rank where to look first. They are heuristic priorities, not probabilities and not proof of a defect.",
  candidates: [{ rank: 1, rel_path: "pkg/calc.py", symbol: "pkg.calc.helper", symbol_stable_id: "x", start_line: 1, end_line: 4, failing_line: 3, score: 5.75,
    confidence: "high", role: "exception_origin", evidence: [], contradicting: [{ id: "e", type: "x", weight: -0.5, detail: "line not executed" }],
    explanation: "Rank 1, score 5.75. The exception surfaced here (+5)." }], ...over,
});
const inv = (over = {}) => ({
  id: "i1", failure_id: "f1", status: "open", resolution: null, generator_version: "1.0", index_snapshot_id: "s1", created_at: "2026-10-18T10:00:00Z",
  updated_at: "2026-10-18T10:00:00Z", failure_summary: "ValueError", notes: "",
  content: {
    summary: "s", observations: [{ kind: "fact", text: "The stack has 2 frame(s).", ref: "frames" }], candidates: [], localization_state: "ranked",
    hypotheses: [{ id: "H-origin", kind: "hypothesis", status: "unverified", statement: "The code at the failing location mishandles its input.",
      supporting: [{ text: "Exception surfaced on this line.", ref: "e" }], contradicting: [{ text: "A caller may have passed invalid input.", ref: "frames" }], verification: ["Reproduce it alone."] }],
    missing_information: ["The failure has been seen once."], verification_steps: [], next_tests: [], related_findings: [], related_failures: [], impact: null,
    limitations: ["No AI provider was used."],
  }, verifications: [], ...over,
});
const run = (over = {}) => ({ id: "x", kind: "existing", status: "failed", reason: null, exit_code: 1, tests_collected: 2, tests_passed: 1, tests_failed: 1,
  tests_errors: 0, tests_skipped: 0, finished_at: null, failures: [], ...over });
const ver = (outcome: string, over = {}) => ({ id: "v1", outcome, meaning: outcome === "NOT_REPRODUCED_IN_VERIFICATION_RUN" ? "The selected tests passed in this run. This is not proof of a fix." : "Waiting.",
  reason: null, test_ids: ["tests/test_a.py::test_exc"], created_at: "2026-10-18T12:00:00Z", evaluated_at: null,
  original_run: run({ id: "o" }), verification_run: run({ id: "n", status: outcome === "PENDING" ? "running" : "passed", tests_failed: 0, tests_passed: 2 }), baseline_available: true, ...over });
const base = { "GET /ready": readyOk, [`GET ${BASE}`]: { body: repoDetail } };

describe("failure inbox", () => {
  it("shows a loading state and then the failures with recurrence", async () => {
    mockApi({ ...base, [`GET ${BASE}/failures`]: page([failure()]) });
    renderApp(`${BASE}/failures`);
    expect(await screen.findByText("Loading failures…")).toBeInTheDocument();
    expect(await screen.findByText("ValueError: bad value 0")).toBeInTheDocument();
    expect(screen.getByText("2× seen")).toBeInTheDocument();
    expect(screen.getByText(/pkg\/calc\.py:3/)).toBeInTheDocument();
  });

  it("has an honest empty state, and a different one when filters exclude everything", async () => {
    mockApi({ ...base, [`GET ${BASE}/failures`]: page([]) });
    renderApp(`${BASE}/failures`);
    expect(await screen.findByText("No failures recorded")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Search"), "zzz");
    expect(await screen.findByText("No failures match these filters")).toBeInTheDocument();
  });

  it("shows an error state with retry", async () => {
    mockApi({ ...base, [`GET ${BASE}/failures`]: { status: 404, body: { error: { code: "not_found", message: "Repository not found." } } } });
    renderApp(`${BASE}/failures`);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("sends the filters as query parameters and ingests pasted text", async () => {
    const { calls, fn } = mockApi({ ...base, [`GET ${BASE}/failures`]: page([failure()]),
      [`POST ${BASE}/failures/ingest`]: { status: 201, body: { items: [failure()], created: 1, recurred: 0 } } });
    renderApp(`${BASE}/failures`);
    await screen.findByText("ValueError: bad value 0");
    await userEvent.selectOptions(screen.getByLabelText("Status"), "open");
    await waitFor(() => expect(fn.mock.calls.some(([u]) => String(u).includes("status=open"))).toBe(true));
    await userEvent.type(screen.getByLabelText("Paste the failure text"), "E   ValueError: x");
    await userEvent.click(screen.getByRole("button", { name: "Add failure" }));
    expect(await screen.findByText(/1 new failure\(s\), 0 recurrence\(s\)/)).toBeInTheDocument();
    expect(calls.find((c) => c.key.startsWith("POST"))?.body).toEqual({ source: "pytest_text", text: "E   ValueError: x" });
  });
});

describe("failure details, localization and investigation", () => {
  const detailRoutes = (d = detail(), l = loc()) => ({ ...base, [`GET ${BASE}/failures/f1`]: { body: d }, [`GET ${BASE}/failures/f1/localization`]: { body: l } });

  it("labels the ranking as heuristic and lists missing evidence", async () => {
    mockApi(detailRoutes());
    renderApp(`${BASE}/failures/f1`);
    expect(await screen.findByText(/not probabilities and not proof/)).toBeInTheDocument();
    expect(screen.getByText("Suspect locations (heuristic ranking)")).toBeInTheDocument();
    expect(screen.getByText("high confidence")).toBeInTheDocument();
    expect(screen.getByText("Coverage was not measured.")).toBeInTheDocument();
    expect(screen.getByText(/Against: line not executed/)).toBeInTheDocument();
    expect(screen.getByText("unknown")).toBeInTheDocument(); // revision is never invented
    expect(screen.getByLabelText("Sanitized error message")).toHaveTextContent("[REDACTED]");
  });

  it("says insufficient evidence instead of showing a guess", async () => {
    mockApi(detailRoutes(detail({ frames: [] }), loc({ state: "insufficient_evidence", candidates: [], missing_evidence: ["No stack frame maps to a repository file."] })));
    renderApp(`${BASE}/failures/f1`);
    expect(await screen.findByText(/Insufficient evidence to rank a location/)).toBeInTheDocument();
    expect(screen.getByText(/No stack frames could be parsed/)).toBeInTheDocument();
    expect(screen.queryByText("high confidence")).not.toBeInTheDocument();
  });

  it("shows an error state when the failure does not exist", async () => {
    mockApi({ ...base, [`GET ${BASE}/failures/f1`]: { status: 404, body: { error: { code: "not_found", message: "Failure not found." } } } });
    renderApp(`${BASE}/failures/f1`);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("creates an investigation on request, not automatically", async () => {
    const { calls } = mockApi({ ...detailRoutes(), [`POST ${BASE}/failures/f1/investigations`]: { status: 201, body: inv() } });
    renderApp(`${BASE}/failures/f1`);
    await userEvent.click(await screen.findByRole("button", { name: "Start investigation" }));
    await waitFor(() => expect(calls.some((c) => c.key === `POST ${BASE}/failures/f1/investigations`)).toBe(true));
  });

  it("keeps facts and hypotheses visibly separate", async () => {
    mockApi({ ...detailRoutes(detail({ investigation_id: "i1", investigation_status: "open" })), [`GET ${BASE}/investigations/i1`]: { body: inv() },
      [`GET ${BASE}/investigations/i1/verifications`]: { body: { items: [], outcomes: [] } } });
    renderApp(`${BASE}/failures/f1`);
    const facts = await screen.findByRole("region", { name: "Observed facts" });
    expect(within(facts).getByText("The stack has 2 frame(s).")).toBeInTheDocument();
    expect(within(facts).queryByText(/mishandles its input/)).not.toBeInTheDocument();
    const hyp = screen.getByRole("region", { name: "Hypotheses" });
    expect(within(hyp).getByText("unverified")).toBeInTheDocument();
    const card = within(hyp).getByLabelText("Hypothesis H-origin");
    expect(within(card).getByText("Exception surfaced on this line.")).toBeInTheDocument();
    expect(within(card).getByText("A caller may have passed invalid input.")).toBeInTheDocument();
    expect(screen.getByText("The failure has been seen once.")).toBeInTheDocument();
    expect(await screen.findByText("No verification has been run.")).toBeInTheDocument();
  });
});

describe("verification", () => {
  it("previews first, runs only after approval, and shows no success until the backend reports it", async () => {
    let started = false;
    let outcome = "PENDING";
    const { calls } = mockApi({
      ...base,
      [`GET ${BASE}/failures/f1`]: { body: detail({ investigation_id: "i1", investigation_status: "open" }) },
      [`GET ${BASE}/failures/f1/localization`]: { body: loc() },
      [`GET ${BASE}/investigations/i1`]: { body: inv() },
      [`GET ${BASE}/investigations/i1/verifications`]: () => ({ body: { items: started ? [ver(outcome)] : [], outcomes: [] } }),
      [`POST ${BASE}/investigations/i1/verification`]: (init) => {
        const body = JSON.parse(String(init?.body));
        if (!body.confirm) return { body: { executes: false, requires: "confirm", targets: ["tests/test_a.py::test_exc"], default_targets: [], coverage: false,
          execution: "restricted subprocess on this machine; not a sandbox.", baseline_execution_run_id: "o", comparison: "x", investigation_id: "i1" } };
        started = true;
        return { status: 202, body: { ...ver("PENDING"), started: true } };
      },
    });
    renderApp(`${BASE}/failures/f1`);
    await userEvent.click(await screen.findByRole("button", { name: "Review verification run" }));
    expect(await screen.findByText(/Nothing has run yet/)).toBeInTheDocument();
    expect(screen.getByText(/not a sandbox/)).toBeInTheDocument();
    expect(calls.filter((c) => c.key.startsWith("POST") && (c.body as { confirm?: boolean }).confirm).length).toBe(0);
    await userEvent.click(screen.getByRole("button", { name: "Approve and run" }));
    expect(await screen.findByLabelText("Verification PENDING")).toBeInTheDocument();
    expect(screen.queryByText("Not reproduced in this run")).not.toBeInTheDocument();
    outcome = "NOT_REPRODUCED_IN_VERIFICATION_RUN";
    expect(await screen.findByText("Not reproduced in this run", {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getByText(/not proof of a fix/)).toBeInTheDocument();
    expect(screen.queryByText(/permanently fixed/i)).not.toBeInTheDocument();
    expect(screen.getByText("Original run")).toBeInTheDocument();
    expect(screen.getByText("Verification run")).toBeInTheDocument();
  }, 15000);

  it("shows a persisting failure and an inconclusive result without calling either success", async () => {
    mockApi({
      ...base,
      [`GET ${BASE}/failures/f1`]: { body: detail({ investigation_id: "i1", investigation_status: "open" }) },
      [`GET ${BASE}/failures/f1/localization`]: { body: loc() },
      [`GET ${BASE}/investigations/i1`]: { body: inv() },
      [`GET ${BASE}/investigations/i1/verifications`]: { body: { items: [ver("PERSISTS", { id: "a", meaning: "The same failure occurred again." }),
        ver("INCONCLUSIVE", { id: "b", reason: "The run could not start.", verification_run: null })], outcomes: [] } },
    });
    renderApp(`${BASE}/failures/f1`);
    expect(await screen.findByText("Failure persists")).toBeInTheDocument();
    expect(screen.getByText("Inconclusive")).toBeInTheDocument();
    expect(screen.getByText("The run could not start.")).toBeInTheDocument();
    expect(screen.getByText("Not available.")).toBeInTheDocument();
  });
});
