import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { getStoredToken, setStoredToken } from "../services/api";
import { mockApi, readyOk, renderApp } from "./helpers";

const unauthorized = { status: 401, body: { error: { code: "unauthorized", message: "A valid bearer token is required." } } };
const overview = { body: { repositories: 1, indexed_repositories: 1, indexed_files: 270, tasks_total: 1, tasks_by_status: {}, recent_events: [] } };
const bearer = (init: RequestInit | undefined) => new Headers(init?.headers).get("Authorization");

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("sign-in", () => {
  it("shows a sign-in screen, not the app, when the API needs a token; no guest option unless the build enables it", async () => {
    mockApi({ "GET /ready": readyOk, "GET /overview": unauthorized });
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "Sign in to NOVA" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Primary" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Explore the live demo" })).toBeNull();
  });

  it("starts a guest session on the hosted demo and then shows the app with that token", async () => {
    vi.stubEnv("VITE_GUEST_ACCESS", "true");
    const { calls } = mockApi({
      "GET /ready": readyOk,
      "GET /overview": (init) => (bearer(init) === "Bearer nova_u_guest" ? overview : unauthorized),
      "POST /auth/guest": { body: { token: "nova_u_guest", role: "engineer", handle: "guest" } },
    });
    renderApp("/");
    await userEvent.click(await screen.findByRole("button", { name: "Explore the live demo" }));
    expect(await screen.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    expect(getStoredToken()).toBe("nova_u_guest");
    expect(calls.filter((c) => c.key === "POST /auth/guest")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });

  it("forgets a stale token (for example after a demo restart) and asks to sign in again", async () => {
    setStoredToken("nova_u_expired");
    mockApi({ "GET /ready": readyOk, "GET /overview": unauthorized });
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "Sign in to NOVA" })).toBeInTheDocument();
    expect(getStoredToken()).toBe("");
  });

  it("accepts an access key only after the API confirms it", async () => {
    mockApi({
      "GET /ready": readyOk,
      "GET /overview": (init) => (bearer(init) === "Bearer owner-key" ? overview : unauthorized),
      "GET /workspace": (init) => (bearer(init) === "Bearer owner-key" ? { body: {} } : unauthorized),
    });
    renderApp("/");
    const key = await screen.findByLabelText("Access key");
    await userEvent.type(key, "wrong-key");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("A valid bearer token is required.");
    expect(getStoredToken()).toBe("");
    await userEvent.clear(key);
    await userEvent.type(key, "owner-key");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(screen.getByRole("navigation", { name: "Primary" })).toBeInTheDocument());
    expect(getStoredToken()).toBe("owner-key");
  });

  it("signs out back to the sign-in screen", async () => {
    setStoredToken("owner-key");
    mockApi({ "GET /ready": readyOk, "GET /overview": overview });
    renderApp("/");
    await userEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("heading", { name: "Sign in to NOVA" })).toBeInTheDocument();
    expect(getStoredToken()).toBe("");
  });
});
