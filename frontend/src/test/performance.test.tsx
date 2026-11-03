import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../app/App";
import { api } from "../services/api";
import { mockApi, renderApp } from "./helpers";

afterEach(() => vi.unstubAllGlobals());

describe("performance behaviour", () => {
  it("splits every page route into a lazily loaded chunk (only the layout is eager)", () => {
    const children = routes[0]?.children ?? [];
    expect(children.length).toBeGreaterThan(20);
    for (const r of children) {
      expect(typeof r.lazy).toBe("function");
      expect(r.element).toBeUndefined();
    }
  });

  it("lazy routes still render their page with loading → ready", async () => {
    mockApi({ "GET /repositories": { body: { items: [], total: 0, page: 1, page_size: 20, pages: 0 } } });
    renderApp("/repositories");
    expect(await screen.findByRole("heading", { name: /repositories/i })).toBeInTheDocument();
  });

  it("an aborted request is surfaced as an abort, never as a 'cannot reach the API' error", async () => {
    vi.stubGlobal("fetch", vi.fn(async (_u: unknown, init?: RequestInit) => {
      await new Promise((_, rej) => init?.signal?.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError"))));
    }));
    const ctl = new AbortController();
    const p = api.listSymbols("r1", { q: "a" }, ctl.signal);
    ctl.abort();
    await expect(p).rejects.toMatchObject({ name: "AbortError" });
  });

  it("passes the abort signal to the network layer for list requests", async () => {
    const seen: (AbortSignal | null | undefined)[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_u: unknown, init?: RequestInit) => {
      seen.push(init?.signal);
      return new Response(JSON.stringify({ items: [], total: 0, page: 1, page_size: 25, pages: 0 }), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    const ctl = new AbortController();
    await api.listFiles("r1", { page: 1, pageSize: 25 }, ctl.signal);
    await api.listSymbols("r1", { q: "x" }, ctl.signal);
    expect(seen).toEqual([ctl.signal, ctl.signal]);
  });

  it("debounces symbol search so a burst of keystrokes issues one request", async () => {
    const calls: string[] = [];
    mockApi({
      "GET /repositories/r1": { body: { id: "r1", name: "r", root_path: "/r", status: "indexed", created_at: "2026-01-01T00:00:00Z" } },
      "GET /repositories/r1/symbols": (_i, url) => { calls.push(url.searchParams.get("q") ?? ""); return { body: { items: [], total: 0, page: 1, page_size: 20, pages: 0 } }; },
    });
    renderApp("/repositories/r1/intelligence");
    const box = await screen.findByLabelText(/filter symbols/i);
    await userEvent.type(box, "abcd");
    await waitFor(() => expect(calls).toContain("abcd"), { timeout: 2000 });
    expect(calls.filter((c) => c !== "" && c !== "abcd")).toEqual([]);
  });
});
