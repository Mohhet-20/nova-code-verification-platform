import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, readyOk, renderApp } from "./helpers";
import { repoDetail, snapshot } from "./fixtures";

afterEach(() => vi.unstubAllGlobals());

const BASE = "/repositories/r1";
const idx = { snapshot_id: "snap1234abcd", generation: 3, status: "completed", finished_at: "2026-10-18T10:00:02Z" };
const status = (over: Record<string, unknown> = {}) => ({
  body: { state: "completed", serving: idx, latest: snapshot, last_failure: null, message: "The index is up to date with its last run.",
    pending: null, files_awaiting_indexing: 0, analysis_version: 2, ...over },
});
const metrics = (over: Record<string, unknown> = {}) => ({
  body: { indexed: true, index: idx, generated_at: "2026-10-18T10:00:00Z", files: 42, files_by_language: { Python: 30 },
    files_by_analysis_status: { parsed: 30 }, parse_failures: 1, symbols: 120, symbols_by_kind: { function: 80 },
    edges_by_type: { IMPORTS_FILE: 10, CALLS: 30 }, imports_by_status: { resolved: 10, unresolved: 3, unsupported: 1 },
    calls_by_status: { resolved: 30 }, cycles: 2, max_dependency_depth: 6, files_awaiting_indexing: null,
    definitions: { files: "Files in the published index.", symbols: "Definitions.", max_dependency_depth: "Longest chain.", parse_failures: "Parser failed." },
    ...over },
});
const sym = { id: "s1", stable_id: "a".repeat(32), name: "charge", qualified_name: "pay.charge", kind: "function", language: "Python",
  file_id: "f1", file_path: "src/pay.py", start_line: 12, start_col: 0, end_line: 20, end_col: 0, parent_stable_id: null,
  visibility: null, exported: true, signature: "def charge(x)", doc_summary: null, bases: [], parser: "tree-sitter-python",
  extraction_method: "ast", confidence: "high" };
const page = (items: unknown[]) => ({ items, total: items.length, page: 1, page_size: 20, pages: 1 });
const detail = { ...sym, children: [], callers: [{ stable_id: "b".repeat(32), name: "checkout", qualified_name: "shop.checkout", kind: "function", path: "src/shop.py", line: 5, relationship: "CALLS", status: "resolved", evidence: "inferred" }],
  callees: [], base_links: [], subclasses: [], caller_total: 1, callee_total: 0, unresolved_call_total: 2, index: idx, note: "Callers and callees come from name-based static resolution and may be incomplete." };
const item = (path: string, depth: number, extra: Record<string, unknown> = {}) => ({ kind: "file", id: path, name: path.split("/").pop(), path, depth,
  relationship: "IMPORTS_FILE", status: "resolved", evidence: "observed", line: 3, chain: ["src/a.py", path], explanation: `${path} imports src/a.py directly.`, ...extra });
const depsBody = (items: unknown[], extra: Record<string, unknown> = {}) => ({ ...page(items), root: { file_id: "f1", path: "src/a.py" }, direction: "out",
  max_depth: 2, truncated: false, more_beyond_depth: false, unresolved: [], unresolved_total: 0, index: idx, note: "Edges are resolved file-to-file imports.", ...extra });
const base = { "GET /ready": readyOk, [`GET ${BASE}`]: { body: repoDetail }, [`GET ${BASE}/summary`]: { body: { repository_id: "r1", indexed: true, snapshot, top_languages: [], largest_files: [], symbol_kinds: [], index_state: "ready" } } };

describe("intelligence workspace", () => {
  it("shows overview cards from real metrics and a symbol list", async () => {
    mockApi({ ...base, [`GET ${BASE}/index/status`]: status(), [`GET ${BASE}/metrics`]: metrics(), [`GET ${BASE}/symbols`]: { body: page([sym]) } });
    renderApp(`${BASE}/intelligence`);
    const overview = await screen.findByRole("region", { name: "Index overview" });
    expect(within(overview).getByText("42")).toBeInTheDocument();
    expect(within(overview).getByText("120")).toBeInTheDocument();
    expect(within(overview).getByText("Dependency cycles")).toBeInTheDocument();
    expect(within(overview).getByText("IMPORTS_FILE 10 · CALLS 30")).toBeInTheDocument();
    expect(within(overview).getByText("4")).toBeInTheDocument(); // unresolved + dynamic imports
    expect(await screen.findByText("charge")).toBeInTheDocument();
    expect(screen.getByText(/awaiting indexing|no files awaiting/)).toBeInTheDocument();
  });

  it("shows a loading state, then an explicit error with retry when metrics fail", { timeout: 12_000 }, async () => {
    mockApi({ ...base, [`GET ${BASE}/index/status`]: status(), [`GET ${BASE}/symbols`]: { body: page([]) },
      [`GET ${BASE}/metrics`]: { status: 500, body: { error: { code: "internal_error", message: "An internal error occurred." } } } });
    renderApp(`${BASE}/intelligence`);
    expect(await screen.findByText("Loading metrics…")).toBeInTheDocument();
    expect((await screen.findAllByRole("alert", {}, { timeout: 8000 }))[0]).toHaveTextContent("An internal error occurred.");
    expect(screen.getAllByRole("button", { name: "Try again" }).length).toBeGreaterThan(0);
  });

  it("shows an empty state when the repository is not indexed", async () => {
    mockApi({ ...base, [`GET ${BASE}/index/status`]: status({ state: "never_indexed", serving: null, latest: null, message: "This repository has not been indexed.", files_awaiting_indexing: null }),
      [`GET ${BASE}/metrics`]: { body: { indexed: false, index: null, generated_at: "2026-10-18T10:00:00Z", files: 0, definitions: {}, cycles: 0, max_dependency_depth: 0, parse_failures: 0, symbols: 0, edges_by_type: {}, imports_by_status: {}, calls_by_status: {}, files_by_language: {}, files_by_analysis_status: {}, symbols_by_kind: {}, files_awaiting_indexing: null } } });
    renderApp(`${BASE}/intelligence`);
    expect(await screen.findByText("Nothing to explore yet")).toBeInTheDocument();
    expect(screen.getByText(/no published index yet/)).toBeInTheDocument();
  });

  it("opens symbol detail with callers and hands off to the impact panel", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi({ ...base, [`GET ${BASE}/index/status`]: status(), [`GET ${BASE}/metrics`]: metrics(),
      [`GET ${BASE}/symbols`]: { body: page([sym]) }, [`GET ${BASE}/symbols/${sym.stable_id}`]: { body: detail },
      [`GET ${BASE}/dependencies/impact`]: (_i, url) => ({ body: { ...page([item("src/shop.py", 1, { kind: "function", name: "shop.checkout", relationship: "CALLS", evidence: "inferred", explanation: "shop.checkout calls pay.charge." })]),
        target_kind: "symbol", target_id: sym.stable_id, target_name: "pay.charge", target_path: "src/pay.py", max_depth: Number(url.searchParams.get("max_depth")),
        summary: { direct: 1, transitive: 0, total: 1, files_affected: 1, max_depth_reached: 1 }, truncated: false, more_beyond_depth: false, cycles: [],
        disclaimer: "Static analysis estimate. It follows imports.", index: idx } }) });
    renderApp(`${BASE}/intelligence`);
    await user.click(await screen.findByRole("button", { name: /charge/ }));
    expect(await screen.findByText("shop.checkout")).toBeInTheDocument();
    expect(screen.getByText(/2 call site\(s\)/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Analyse impact" }));
    expect(await screen.findByText(/Static analysis estimate/)).toBeInTheDocument();
    expect(screen.getByText("shop.checkout calls pay.charge.")).toBeInTheDocument();
    const call = calls.find((c) => c.key.endsWith("/dependencies/impact"));
    expect(call).toBeDefined();
  });

  it("explores dependencies by path, shows unresolved imports and the beyond-depth notice", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi({ ...base, [`GET ${BASE}/index/status`]: status(), [`GET ${BASE}/metrics`]: metrics(), [`GET ${BASE}/symbols`]: { body: page([]) },
      [`GET ${BASE}/dependencies`]: { body: depsBody([item("src/b.py", 1)], { more_beyond_depth: true, unresolved_total: 1,
        unresolved: [{ specifier: "./gone", kind: "import", status: "unresolved", note: "no such file", line: 4, package: null }] }) } });
    renderApp(`${BASE}/intelligence`);
    await user.click(await screen.findByRole("tab", { name: "Dependencies" }));
    await user.type(screen.getByLabelText(/File path/), "src/a.py");
    await user.click(screen.getByRole("button", { name: "Explore" }));
    expect(await screen.findByText("src/b.py")).toBeInTheDocument();
    expect(screen.getByText(/More files exist beyond this depth/)).toBeInTheDocument();
    expect(screen.getByText("./gone")).toBeInTheDocument();
    expect(screen.getByText(/Imports that could not be resolved \(1\)/)).toBeInTheDocument();
    expect(calls.some((c) => c.key === `GET ${BASE}/dependencies`)).toBe(true);
  });

  it("surfaces a rejected path as an error instead of an empty list", async () => {
    const user = userEvent.setup();
    mockApi({ ...base, [`GET ${BASE}/index/status`]: status(), [`GET ${BASE}/metrics`]: metrics(), [`GET ${BASE}/symbols`]: { body: page([]) },
      [`GET ${BASE}/dependencies`]: { status: 400, body: { error: { code: "unsafe_path", message: "The path must be a repository-relative path without '..' segments." } } } });
    renderApp(`${BASE}/intelligence`);
    await user.click(await screen.findByRole("tab", { name: "Dependencies" }));
    await user.type(screen.getByLabelText(/File path/), "../etc/passwd");
    await user.click(screen.getByRole("button", { name: "Explore" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("repository-relative");
  });

  it("warns about cycles in an impact result", async () => {
    const user = userEvent.setup();
    mockApi({ ...base, [`GET ${BASE}/index/status`]: status(), [`GET ${BASE}/metrics`]: metrics(), [`GET ${BASE}/symbols`]: { body: page([]) },
      [`GET ${BASE}/dependencies/impact`]: { body: { ...page([item("src/y.py", 1)]), target_kind: "file", target_id: "f1", target_name: "x.py", target_path: "src/x.py", max_depth: 5,
        summary: { direct: 1, transitive: 0, total: 1, files_affected: 1, max_depth_reached: 1 }, truncated: true, more_beyond_depth: false,
        cycles: [{ id: "c1", size: 3, severity: "medium", files: ["src/x.py"] }], disclaimer: "Static analysis estimate.", index: idx } } });
    renderApp(`${BASE}/intelligence`);
    await user.click(await screen.findByRole("tab", { name: "Impact" }));
    await user.type(screen.getByLabelText("File to change"), "src/x.py");
    await user.click(screen.getByRole("button", { name: "Analyse impact" }));
    expect(await screen.findByText(/structural dependency cycle \(3 files, medium\)/)).toBeInTheDocument();
    expect(screen.getByText(/hit its node limit/)).toBeInTheDocument();
  });

  it("lists cycles with severity, and shows an empty state when there are none", async () => {
    const user = userEvent.setup();
    const cyc = { id: "c1", kind: "structural_dependency_cycle", size: 3, severity: "medium", severity_reason: "3 files import each other (directly or transitively)",
      files: [{ file_id: "1", path: "cyc/x.py" }, { file_id: "2", path: "cyc/y.py" }], files_truncated: false, edges: [], explanation: "Following imports can lead back." };
    let empty = false;
    mockApi({ ...base, [`GET ${BASE}/index/status`]: status(), [`GET ${BASE}/metrics`]: metrics(), [`GET ${BASE}/symbols`]: { body: page([]) },
      [`GET ${BASE}/dependencies/cycles`]: () => ({ body: { ...page(empty ? [] : [cyc]), severity_rules: {}, note: "A structural dependency cycle is a coupling smell.", index: idx } }) });
    renderApp(`${BASE}/intelligence`);
    await user.click(await screen.findByRole("tab", { name: "Cycles" }));
    expect(await screen.findByText("3-file cycle")).toBeInTheDocument();
    expect(screen.getByText("cyc/x.py")).toBeInTheDocument();
    expect(screen.getByText(/coupling smell/)).toBeInTheDocument();
    empty = true;
  });

  it("requires confirmation before a rebuild and blocks double submission", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi({ ...base, [`GET ${BASE}/index/status`]: status(), [`GET ${BASE}/metrics`]: metrics(), [`GET ${BASE}/symbols`]: { body: page([]) },
      [`POST ${BASE}/index/rebuild`]: { status: 202, body: { ...snapshot, status: "running", mode: "full" } } });
    renderApp(`${BASE}/intelligence`);
    await user.click(await screen.findByRole("button", { name: "Rebuild index" }));
    expect(calls.some((c) => c.key.startsWith("POST"))).toBe(false); // first click only asks
    expect(screen.getByText(/re-parses every file/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Confirm rebuild" }));
    await waitFor(() => expect(calls.filter((c) => c.key === `POST ${BASE}/index/rebuild`)).toHaveLength(1));
    expect(screen.queryByRole("button", { name: "Confirm rebuild" })).not.toBeInTheDocument(); // confirmation closes
  });

  it("shows running state, disables controls, and reports a failed run honestly", async () => {
    mockApi({ ...base, [`GET ${BASE}/index/status`]: status({ state: "running", message: "An indexing run is in progress; queries use the previous generation." }),
      [`GET ${BASE}/metrics`]: metrics(), [`GET ${BASE}/symbols`]: { body: page([]) } });
    renderApp(`${BASE}/intelligence`);
    expect(await screen.findByText(/Indexing in progress/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Rebuild index" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Indexing…" })).toBeDisabled();
  });

  it("explains a failed latest run while the previous generation still serves", async () => {
    mockApi({ ...base, [`GET ${BASE}/index/status`]: status({ state: "failed", last_failure: "Unexpected error.", message: "The latest run failed and queries are served from the previous generation." }),
      [`GET ${BASE}/metrics`]: metrics(), [`GET ${BASE}/symbols`]: { body: page([]) } });
    renderApp(`${BASE}/intelligence`);
    expect(await screen.findByText(/Last run failed: Unexpected error\./)).toBeInTheDocument();
    expect(screen.getByText(/Queries still use the previous generation/)).toBeInTheDocument();
  });
});
