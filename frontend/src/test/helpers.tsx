import { QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { vi } from "vitest";
import { routes, makeQueryClient } from "../app/App";

type Handler = (init: RequestInit | undefined, url: URL) => { status?: number; body: unknown };
export type Routes = Record<string, Handler | { status?: number; body: unknown }>;

/** Stub fetch with per-"METHOD /path" responses. Unmatched requests fail the test loudly. */
export function mockApi(table: Routes) {
  const calls: { key: string; body: unknown }[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const key = `${init?.method ?? "GET"} ${url.pathname.replace("/api/v1", "")}`;
    calls.push({ key, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const entry = table[key];
    if (!entry) throw new Error(`Unmocked request: ${key}`);
    const res = typeof entry === "function" ? entry(init, url) : entry;
    return new Response(JSON.stringify(res.body), {
      status: res.status ?? 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fn);
  return { calls, fn };
}

export function renderApp(path: string, ui?: ReactElement) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  return render(
    <QueryClientProvider client={makeQueryClient()}>{ui ?? <RouterProvider router={router} />}</QueryClientProvider>,
  );
}

export const readyOk = { status: 200, body: { status: "ready", checks: { database: "ok", migrations: "ok" } } };
