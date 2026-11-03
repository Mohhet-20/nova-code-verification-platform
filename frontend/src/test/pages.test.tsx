import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { plan, repoDetail, snapshot, task } from "./fixtures";
import { mockApi, readyOk, renderApp } from "./helpers";

afterEach(() => vi.unstubAllGlobals());

const emptyPage = { items: [], total: 0, page: 1, page_size: 20, pages: 1 };
const fresh = { state: "fresh", snapshot_id: "snap1234abcd", generation: 1, indexed_at: null, last_attempt_status: "completed", changed: 0, new: 0, removed: 0, sample_paths: [], git_head_changed: null, note: "" };
const fileRow = { id: "f1", change_status: "new", analysis_status: "parsed", language_confidence: "high", parser: "tree-sitter-python", symbol_count: 4, import_count: 1, rel_path: "src/a.py", extension: ".py", language: "Python", size_bytes: 10, line_count: 2, depth: 1 };

describe("dashboard", () => {
  it("shows real counts from the API, not placeholders", async () => {
    mockApi({
      "GET /ready": readyOk,
      "GET /overview": { body: { repositories: 3, indexed_repositories: 2, indexed_files: 1234, tasks_total: 5,
        tasks_by_status: { planned: 5 }, recent_events: [{ id: "e1", action: "index.completed", entity_type: "snapshot", entity_id: "s", details: {}, created_at: "2026-10-18T10:00:00Z" }] } },
    });
    renderApp("/");
    expect(await screen.findByText("1,234")).toBeInTheDocument();
    expect(screen.getByText("Indexing completed")).toBeInTheDocument();
    expect(screen.getByText("5 planned")).toBeInTheDocument();
  });

  it("shows an empty state when nothing is registered", async () => {
    mockApi({ "GET /ready": readyOk, "GET /overview": { body: { repositories: 0, indexed_repositories: 0, indexed_files: 0, tasks_total: 0, tasks_by_status: {}, recent_events: [] } } });
    renderApp("/");
    expect(await screen.findByText("No repositories yet")).toBeInTheDocument();
  });

  it("shows an error state with retry when the API fails", { timeout: 10_000 }, async () => {
    mockApi({ "GET /ready": readyOk, "GET /overview": { status: 500, body: { error: { code: "internal_error", message: "An internal error occurred." } } } });
    renderApp("/");
    // 5xx responses are retried twice with backoff (about 3s) before the error state appears.
    expect(await screen.findByRole("alert", {}, { timeout: 6000 })).toHaveTextContent("An internal error occurred.");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});

describe("repositories", () => {
  it("lists repositories without exposing full paths", async () => {
    mockApi({
      "GET /ready": readyOk,
      "GET /repositories": { body: { ...emptyPage, total: 1, items: [{ id: "r1", name: "payments", description: "", root_label: "payments-dir", is_git: true, git_branch: "main", created_at: "2026-10-18T09:00:00Z", latest_snapshot_status: "completed", latest_snapshot_at: null, files_indexed: 42 }] } },
    });
    renderApp("/repositories");
    expect(await screen.findByRole("link", { name: "payments" })).toHaveAttribute("href", "/repositories/r1");
    expect(screen.getByText("payments-dir")).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
  });

  it("surfaces backend validation errors on registration", async () => {
    const user = userEvent.setup();
    mockApi({
      "GET /ready": readyOk,
      "POST /repositories": { status: 422, body: { error: { code: "invalid_repository", message: "Repository path does not exist." } } },
    });
    renderApp("/repositories/new");
    await user.type(await screen.findByLabelText("Display name"), "demo");
    await user.type(screen.getByLabelText(/Absolute path/), "/nope");
    await user.click(screen.getByRole("button", { name: "Register" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Repository path does not exist.");
  });

  it("registers and navigates to the new repository", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi({
      "GET /ready": readyOk,
      "POST /repositories": { status: 201, body: repoDetail },
      "GET /repositories/r1": { body: repoDetail },
      "GET /repositories/r1/summary": { body: { repository_id: "r1", indexed: true, snapshot, top_languages: [["Python", 30]], largest_files: [["a.py", 100]], symbol_kinds: [["class", 3]], index_state: "ready" } },
      "GET /repositories/r1/freshness": { body: fresh },
      "GET /repositories/r1/files": { body: { items: [fileRow], total: 1, page: 1, page_size: 25, pages: 1 } },
    });
    renderApp("/repositories/new");
    await user.type(await screen.findByLabelText("Display name"), "payments");
    await user.type(screen.getByLabelText(/Absolute path/), "/work/payments");
    await user.click(screen.getByRole("button", { name: "Register" }));
    expect(await screen.findByRole("heading", { name: "payments" })).toBeInTheDocument();
    expect(calls.find((c) => c.key === "POST /repositories")?.body).toEqual({ name: "payments", root_path: "/work/payments", description: "" });
    expect(await screen.findByText("src/a.py")).toBeInTheDocument();
    expect(screen.getByText("Python · 30")).toBeInTheDocument();
  });
});

describe("indexing page", () => {
  it("polls while running and shows results when completed", async () => {
    let n = 0;
    mockApi({
      "GET /ready": readyOk,
      "GET /repositories/r1/snapshots/snap1234abcd": () => (++n < 2
        ? { body: { ...snapshot, status: "running", finished_at: null, files_indexed: 5, skipped_counts: {} } }
        : { body: snapshot }),
      "GET /repositories/r1": { body: repoDetail },
      "GET /repositories/r1/summary": { body: { repository_id: "r1", indexed: true, snapshot, top_languages: [], largest_files: [], symbol_kinds: [], index_state: "ready" } },
      "GET /repositories/r1/freshness": { body: fresh },
      "GET /repositories/r1/snapshots/snap1234abcd/diagnostics": { body: emptyPage },
      "GET /overview": { body: { repositories: 1, indexed_repositories: 1, indexed_files: 42, tasks_total: 0, tasks_by_status: {}, recent_events: [] } },
      "GET /repositories": { body: emptyPage },
      "GET /repositories/r1/files": { body: { ...emptyPage } },
    });
    renderApp("/repositories/r1/indexing/snap1234abcd");
    expect(await screen.findByText(/Scanning… 5 files so far/)).toBeInTheDocument();
    expect(await screen.findByText("Credential-like files (names not recorded)", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.getByText("completed")).toBeInTheDocument();
    expect(screen.getByText("Files indexed").nextSibling).toHaveTextContent("42");
    expect(screen.getByText("Symbols").nextSibling).toHaveTextContent("120");
  });

  it("shows failures", async () => {
    mockApi({ "GET /ready": readyOk, "GET /repositories/r1/snapshots/s": { body: { ...snapshot, status: "failed", error_message: "Repository path does not exist." } },
      "GET /repositories/r1": { body: repoDetail }, "GET /repositories/r1/summary": { body: { repository_id: "r1", indexed: false, snapshot: null, top_languages: [], largest_files: [], symbol_kinds: [], index_state: "ready" } },
      "GET /repositories/r1/freshness": { body: fresh },
      "GET /repositories/r1/snapshots/snap1234abcd/diagnostics": { body: emptyPage },
      "GET /overview": { body: { repositories: 0, indexed_repositories: 0, indexed_files: 0, tasks_total: 0, tasks_by_status: {}, recent_events: [] } },
      "GET /repositories": { body: emptyPage }, "GET /repositories/r1/files": { body: emptyPage } });
    renderApp("/repositories/r1/indexing/s");
    expect(await screen.findByRole("alert")).toHaveTextContent("Indexing failed: Repository path does not exist.");
  });
});

describe("tasks", () => {
  it("validates by relaying backend errors on task creation", async () => {
    const user = userEvent.setup();
    mockApi({
      "GET /ready": readyOk,
      "GET /repositories": { body: { ...emptyPage, total: 1, items: [{ id: "r1", name: "payments", description: "", root_label: "p", is_git: true, git_branch: "main", created_at: "2026-10-18T09:00:00Z", latest_snapshot_status: "completed", latest_snapshot_at: null, files_indexed: 1 }] } },
      "POST /tasks": { status: 422, body: { error: { code: "validation_error", message: "Request validation failed.", details: [{ field: "acceptance_criteria", message: "at least one non-empty acceptance criterion is required" }] } } },
    });
    renderApp("/tasks/new");
    await user.type(await screen.findByLabelText("Title"), "Fix login");
    await user.type(screen.getByLabelText("Problem statement"), "Login is broken for everyone.");
    await user.type(screen.getByLabelText("Objective"), "Make login work");
    await user.type(screen.getByLabelText(/Acceptance criteria/), "works");
    await user.click(screen.getByRole("button", { name: /Create task/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("acceptance_criteria: at least one");
  });

  it("renders the plan returned by the API and clearly labels it as a baseline", async () => {
    mockApi({ "GET /ready": readyOk, "GET /tasks/t1": { body: task } });
    renderApp("/tasks/t1");
    expect(await screen.findByRole("heading", { name: "Harden login" })).toBeInTheDocument();
    expect(screen.getByText(/deterministic baseline, not an AI agent/)).toBeInTheDocument();
    expect(screen.getByDisplayValue("Review the candidate files")).toBeInTheDocument();
    expect(screen.getByDisplayValue("python -m pytest")).toBeInTheDocument();
    expect(screen.getByLabelText("Relevant files, one per line")).toHaveValue("src/auth/login.py");
    expect(screen.getByText("approval")).toBeInTheDocument();
  });

  it("saves plan edits and bumps the revision", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi({
      "GET /ready": readyOk,
      "GET /tasks/t1": { body: task },
      "PUT /tasks/t1/plan": (init) => {
        const sent = JSON.parse(String(init?.body)).content;
        return { body: { ...task, plan: { ...plan, revision: 2, edited_by_user: true, content: sent } } };
      },
      "GET /tasks": { body: emptyPage },
    });
    renderApp("/tasks/t1");
    const title = await screen.findByLabelText("Step 1 title");
    const save = screen.getByRole("button", { name: "Save plan" });
    expect(save).toBeDisabled();
    await user.clear(title);
    await user.type(title, "My step");
    expect(save).toBeEnabled();
    await user.click(save);
    expect(await screen.findByText("Plan saved (revision 2).")).toBeInTheDocument();
    const put = calls.find((c) => c.key === "PUT /tasks/t1/plan");
    expect((put?.body as { content: { steps: { title: string }[] } }).content.steps[0]?.title).toBe("My step");
    expect(screen.getByText(/edited by you/)).toBeInTheDocument();
  });

  it("requires confirmation before regenerating", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi({
      "GET /ready": readyOk, "GET /tasks/t1": { body: task },
      "POST /tasks/t1/plan/regenerate": { body: { ...task, plan: { ...plan, revision: 2 } } }, "GET /tasks": { body: emptyPage },
    });
    renderApp("/tasks/t1");
    await user.click(await screen.findByRole("button", { name: "Regenerate plan" }));
    expect(calls.some((c) => c.key.startsWith("POST"))).toBe(false);
    await user.click(screen.getByRole("button", { name: "Yes, regenerate" }));
    await waitFor(() => expect(calls.some((c) => c.key === "POST /tasks/t1/plan/regenerate")).toBe(true));
  });

  it("removing a step is possible but never leaves zero steps", async () => {
    const user = userEvent.setup();
    mockApi({ "GET /ready": readyOk, "GET /tasks/t1": { body: task } });
    renderApp("/tasks/t1");
    await user.click(await screen.findByRole("button", { name: "Remove step 2" }));
    const remaining = screen.getByRole("button", { name: "Remove step 1" });
    expect(remaining).toBeDisabled();
  });
});

describe("settings & routing", () => {
  it("shows the real security posture from the backend", async () => {
    mockApi({ "GET /ready": readyOk, "GET /workspace": { body: { workspace_id: "w", workspace_name: "Local Workspace", user_handle: "local-dev", user_display_name: "Local Developer", role: "owner", auth_mode: "local-trust", multi_user_isolation: false, ai_provider: "none", planner: "deterministic-baseline", version: "0.1.0", environment: "development", limits: { max_files: 20000 }, registration_restricted: false } } });
    renderApp("/settings");
    const card = (await screen.findByText("Multi-user isolation")).closest("div")!;
    expect(within(card).getByText("Not implemented")).toBeInTheDocument();
    expect(screen.getByText(/max_files=20,000/)).toBeInTheDocument();
  });

  it("renders a not-found page for unknown routes", async () => {
    mockApi({ "GET /ready": readyOk });
    renderApp("/does/not/exist");
    expect(await screen.findByText("Page not found")).toBeInTheDocument();
  });

  it("marks API status offline when readiness fails", async () => {
    mockApi({ "GET /ready": { status: 503, body: { status: "not_ready", checks: {} } } });
    renderApp("/does/not/exist");
    expect(await screen.findByText(/unavailable or not migrated/)).toBeInTheDocument();
  });
});


describe("repository intelligence", () => {
  const summary = { repository_id: "r1", indexed: true, snapshot, top_languages: [["Python", 30], ["Markdown", 12]], largest_files: [], symbol_kinds: [["class", 3]], index_state: "ready" };
  const base = {
    "GET /ready": readyOk,
    "GET /repositories/r1": { body: repoDetail },
    "GET /repositories/r1/summary": { body: summary },
    "GET /repositories/r1/files": { body: { items: [fileRow, { ...fileRow, id: "f2", rel_path: "docs/guide.md", language: "Markdown", analysis_status: "unsupported", parser: null, symbol_count: 0 }], total: 2, page: 1, page_size: 25, pages: 1 } },
  };

  it("shows coverage honestly: parsed vs metadata-only, with symbol and unresolved-import counts", async () => {
    mockApi({ ...base, "GET /repositories/r1/freshness": { body: fresh } });
    renderApp("/repositories/r1");
    expect(await screen.findByText("120")).toBeInTheDocument(); // symbols
    expect(screen.getByText(/\(4 unresolved\)/)).toBeInTheDocument();
    const coverage = screen.getByRole("list", { name: "Analysis coverage" });
    expect(within(coverage).getByText("Parsed · 30")).toBeInTheDocument();
    expect(within(coverage).getByText("Metadata only · 12")).toBeInTheDocument();
    expect(await screen.findByText("Index is current with the files on disk (size and modification time check).")).toBeInTheDocument();
  });

  it("warns when the index is stale", async () => {
    mockApi({ ...base, "GET /repositories/r1/freshness": { body: { ...fresh, state: "stale", changed: 2, new: 1, removed: 0 } } });
    renderApp("/repositories/r1");
    expect(await screen.findByText(/may be out of date: 2 changed, 1 new, 0 removed/)).toBeInTheDocument();
  });

  it("explains completed-with-warnings runs", async () => {
    const warned = { ...snapshot, status: "completed_with_warnings", warnings: ["1 file(s) have syntax errors; symbols are partial."], analysis_counts: { parsed: 29, partial: 1 } };
    mockApi({ ...base, "GET /repositories/r1/summary": { body: { ...summary, snapshot: warned, index_state: "partial" } }, "GET /repositories/r1/freshness": { body: fresh } });
    renderApp("/repositories/r1");
    expect(await screen.findByText("1 file(s) have syntax errors; symbols are partial.")).toBeInTheDocument();
    expect(screen.getByText("Partial (syntax errors) · 1")).toBeInTheDocument();
  });

  it("searches symbols and links each hit to its file, showing why it matched", async () => {
    const user = userEvent.setup();
    const hit = { id: "s1", stable_id: "st1", name: "UserService", qualified_name: "app.users.UserService", kind: "class", language: "Python", file_id: "f1", file_path: "src/a.py", start_line: 7, start_col: 1, end_line: 20, end_col: 1, parent_stable_id: null, visibility: "public", exported: true, signature: null, doc_summary: null, bases: [], parser: "tree-sitter-python", extraction_method: "syntax-tree", confidence: "observed", matched_on: "name_prefix" };
    mockApi({ ...base, "GET /repositories/r1/freshness": { body: fresh },
      "GET /repositories/r1/symbols/search": { body: { items: [hit, { ...hit, id: "s2", file_id: "f3", file_path: "src/b.py" }], total: 2, page: 1, page_size: 20, pages: 1, ambiguous: true, index: { snapshot_id: "snap1234abcd", generation: 1, status: "completed", finished_at: null } } } });
    renderApp("/repositories/r1");
    await user.type(await screen.findByLabelText("Search symbols"), "UserSer");
    const link = await screen.findByRole("link", { name: "src/a.py:7" }, { timeout: 3000 });
    expect(link).toHaveAttribute("href", "/repositories/r1/files/f1");
    expect(screen.getAllByText("name starts with").length).toBe(2);
    expect(screen.getByText(/Several different symbols have exactly this name/)).toBeInTheDocument();
  });

  it("shows file detail with resolution states and never any source", async () => {
    const detail = { id: "f1", rel_path: "src/a.py", extension: ".py", language: "Python", language_confidence: "high", size_bytes: 10, line_count: 2, sha256: "a".repeat(64), change_status: "new", analysis_status: "partial", parser: "tree-sitter-python", analysis_version: 1, parse_diagnostics: ["missing token at line 3"], symbol_count: 1, import_count: 2, dependency_count: 1, dependent_count: 0, index: { snapshot_id: "snap1234abcd", generation: 1, status: "completed", finished_at: null } };
    mockApi({
      "GET /ready": readyOk,
      "GET /repositories/r1/files/f1": { body: detail },
      "GET /repositories/r1/symbols": { body: { items: [{ id: "s1", name: "login", kind: "function", start_line: 1, end_line: 2, exported: true }], total: 1, page: 1, page_size: 200, pages: 1 } },
      "GET /repositories/r1/files/f1/imports": { body: { items: [
        { id: "i1", kind: "import", specifier: "os", names: [], level: 0, line: 1, type_only: false, resolution_status: "external", resolution_note: null, package: "os", resolved_path: null },
        { id: "i2", kind: "import", specifier: "ghost", names: [], level: 0, line: 2, type_only: false, resolution_status: "unresolved", resolution_note: null, package: null, resolved_path: null }], total: 2, page: 1, page_size: 200, pages: 1 } },
      "GET /repositories/r1/files/f1/dependencies": { body: { items: [{ type: "DEPENDS_ON_PACKAGE", status: "external", evidence: "observed", path: null, target_text: "os", symbol_stable_id: null }], total: 1, page: 1, page_size: 200, pages: 1 } },
      "GET /repositories/r1/files/f1/dependents": { body: emptyPage },
    });
    renderApp("/repositories/r1/files/f1");
    expect(await screen.findByRole("heading", { name: "src/a.py" })).toBeInTheDocument();
    expect(screen.getByText("missing token at line 3")).toBeInTheDocument();
    expect(await screen.findByText("External package")).toBeInTheDocument();
    expect(screen.getByText("Unresolved")).toBeInTheDocument();
    expect(await screen.findByText("No dependents found")).toBeInTheDocument();
  });

  it("states plainly that metadata-only languages have no symbols", async () => {
    const detail = { id: "f2", rel_path: "main.go", extension: ".go", language: "Go", language_confidence: "high", size_bytes: 10, line_count: 2, sha256: "b".repeat(64), change_status: "new", analysis_status: "unsupported", parser: null, analysis_version: 1, parse_diagnostics: [], symbol_count: 0, import_count: 0, dependency_count: 0, dependent_count: 0, index: { snapshot_id: "s", generation: 1, status: "completed", finished_at: null } };
    mockApi({ "GET /ready": readyOk, "GET /repositories/r1/files/f2": { body: detail }, "GET /repositories/r1/symbols": { body: emptyPage },
      "GET /repositories/r1/files/f2/imports": { body: emptyPage }, "GET /repositories/r1/files/f2/dependencies": { body: emptyPage }, "GET /repositories/r1/files/f2/dependents": { body: emptyPage } });
    renderApp("/repositories/r1/files/f2");
    expect(await screen.findByText(/no structural parser for Go/)).toBeInTheDocument();
  });

  it("explains a stale file id after re-indexing", async () => {
    mockApi({ "GET /ready": readyOk, "GET /repositories/r1/files/old": { status: 404, body: { error: { code: "not_found", message: "File not found in the current index." } } } });
    renderApp("/repositories/r1/files/old");
    expect(await screen.findByText(/File identifiers change when the repository is re-indexed/)).toBeInTheDocument();
  });

  it("starts a full re-index on request", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi({ ...base, "GET /repositories/r1/freshness": { body: fresh },
      "POST /repositories/r1/index": { status: 202, body: { ...snapshot, id: "newsnap", status: "running" } },
      "GET /repositories/r1/snapshots/newsnap": { body: { ...snapshot, id: "newsnap", status: "running" } } });
    renderApp("/repositories/r1");
    await user.click(await screen.findByRole("button", { name: "Full re-index" }));
    await waitFor(() => expect(calls.find((c) => c.key === "POST /repositories/r1/index")?.body).toEqual({ mode: "full" }));
  });
});
