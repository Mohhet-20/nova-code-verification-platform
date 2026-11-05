/**
 * Optional contract test: renders the real UI in jsdom against a RUNNING backend.
 * Skipped unless NOVA_LIVE_API (e.g. http://127.0.0.1:8000) and NOVA_LIVE_REPO_PATH are set.
 * It registers and indexes a repository and creates a task, so point it at a synthetic repo
 * and a throwaway database.
 */
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { renderApp } from "./helpers";

const base = process.env.NOVA_LIVE_API;
const repoPath = process.env.NOVA_LIVE_REPO_PATH;

describe.skipIf(!base || !repoPath)("live backend", () => {
  const realFetch = globalThis.fetch;
  beforeAll(() => {
    vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) =>
      realFetch(new URL(String(input), base), init),
    );
  });
  afterAll(() => vi.unstubAllGlobals());

  it("registers, indexes, plans and displays real responses", { timeout: 60_000 }, async () => {
    const user = userEvent.setup();

    renderApp("/repositories/new");
    await user.type(await screen.findByLabelText("Display name"), "live-fixture");
    await user.type(screen.getByLabelText(/Absolute path/), repoPath!);
    await user.click(screen.getByRole("button", { name: "Register" }));
    expect(await screen.findByRole("heading", { name: "live-fixture" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Index repository" }));
    expect(await screen.findByText("Credential-like files (names not recorded)", {}, { timeout: 20_000 })).toBeInTheDocument();
    expect(screen.getByText("completed")).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Back to repository" }));
    const table = within(await screen.findByRole("table", { name: "Indexed files" }));
    expect(await table.findByText("src/auth/login.py")).toBeInTheDocument();
    expect(table.getAllByRole("row")).toHaveLength(11); // header + 10 files
    expect(table.queryByText(".env")).not.toBeInTheDocument();
    expect(table.queryByText("deploy/server.pem")).not.toBeInTheDocument();

    // Code navigation against the real index: symbols extracted from the Python fixture.
    await user.type(screen.getByLabelText("Search symbols"), "login");
    const hit = await screen.findByRole("link", { name: /^src\/auth\/login\.py:\d+$/ }, { timeout: 5000 });
    expect(hit).toBeInTheDocument();
    expect(screen.getByText(/exact name/)).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Analysis coverage" })).toHaveTextContent("Parsed");

    // Workspace against the real API: metrics, symbol detail, dependency explorer, cycles.
    await user.click(screen.getByRole("link", { name: "Code intelligence" }));
    const overview = await screen.findByRole("region", { name: "Index overview" }, { timeout: 10_000 });
    expect(within(overview).getByText("Dependency cycles")).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: /^login\b/ }, { timeout: 5000 }));
    expect(await screen.findByRole("button", { name: "Analyse impact" })).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Dependencies" }));
    await user.type(screen.getByLabelText(/File path/), "src/auth/login.py");
    await user.click(screen.getByRole("button", { name: "Explore" }));
    expect(await screen.findByText(/Generation \d+ · \d+ file/, {}, { timeout: 5000 })).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Cycles" }));
    expect(await screen.findByText(/coupling smell/, {}, { timeout: 5000 })).toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Back to repository" }));
    await screen.findByRole("table", { name: "Indexed files" });

    // Workspace against the real API: run analysis, then read the real summary and findings list.
    await user.click(screen.getByRole("link", { name: "Code health" }));
    await user.click(await screen.findByRole("button", { name: "Run analysis" }));
    const health = await screen.findByRole("region", { name: "Code health overview" }, { timeout: 20_000 });
    expect(within(health).getByText("Open findings")).toBeInTheDocument();
    expect(within(health).getByText("No composite quality score is computed.")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Findings" })).toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Back to repository" }));
    await screen.findByRole("table", { name: "Indexed files" });

    await user.click(screen.getByRole("link", { name: "New task" }));
    await user.type(await screen.findByLabelText("Title"), "Rework login validation");
    await user.type(screen.getByLabelText("Problem statement"), "Login accepts any password in src/auth/login.py.");
    await user.type(screen.getByLabelText("Objective"), "Validate passwords before granting a session");
    await user.type(screen.getByLabelText(/Acceptance criteria/), "Wrong password is rejected");
    await user.click(screen.getByRole("button", { name: /Create task/ }));

    expect(await screen.findByRole("heading", { name: "Rework login validation" })).toBeInTheDocument();
    const files = screen.getByLabelText<HTMLTextAreaElement>("Relevant files, one per line");
    expect(files.value.split("\n")[0]).toBe("src/auth/login.py");
    expect(screen.getByDisplayValue("python -m pytest")).toBeInTheDocument();
  });
});
