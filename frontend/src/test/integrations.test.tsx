import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, readyOk, renderApp } from "./helpers";

afterEach(() => vi.unstubAllGlobals());

const page = (items: unknown[]) => ({ body: { items, total: items.length, page: 1, page_size: 20, pages: 1 } });
const err = (status: number, code: string, message: string) => ({ status, body: { error: { code, message } } });
const me = (perms: string[]) => ({ body: { user_id: "u", handle: "admin", display_name: "Admin", org_role: "owner", org_wide_visibility: true, org_permissions: perms, teams: [], org_roles: [], auth_kind: "user_token" } });
const inst = (over = {}) => ({
  id: "i1", integration_type: "github", name: "GitHub main", enabled: false, health: "disabled", connection_status: "unknown", config: {}, has_credential: true, credential_kind: "stored",
  requires_webhook_secret: true, has_webhook_secret: true, capabilities: ["test_connection", "inbound_events"], requested_permissions: ["metadata:read"], granted_permissions: [],
  last_checked_at: null, last_success_at: null, last_sync_at: null, last_error_category: null, created_at: "2026-10-21T09:00:00Z", updated_at: "2026-10-21T09:00:00Z", inbound_path: "/api/v1/integrations/inbound/i1", ...over,
});
const health = (over = {}) => ({
  body: { installations: { disabled: 1 }, total: 1, secrets: { configured: true, stored_credentials_available: true }, outbound_queue: { pending: 0, in_progress: 0, retrying: 0, failed: 0, oldest_due_age_seconds: null, stuck: false },
    jobs: { queued: 0, running: 0, dead: 0, oldest_due_age_seconds: null, stuck: false }, worker_enabled: false, private_destinations_allowed: false, ...over },
});
const catalog = { body: { events: ["workflow.created", "ci.run_recorded"], items: [{ type: "github", title: "GitHub", category: "version_control", description: "Receive PR events", capabilities: ["inbound_events"], permissions: ["metadata:read"], requires_credential: true, requires_webhook_secret: true }] } };

describe("Integration Center", () => {
  it("lists installations with honest health labels and the built-in catalog", async () => {
    mockApi({ "GET /ready": readyOk, "GET /governance/me": me(["*"]), "GET /integrations/installations": page([inst(), inst({ id: "i2", name: "Failing one", enabled: true, health: "failing", last_error_category: "auth_failed" })]), "GET /integrations/health": health({ installations: { failing: 1, disabled: 1 }, total: 2 }), "GET /integrations/catalog": catalog });
    renderApp("/integrations");
    const list = await screen.findByRole("list", { name: "Installations" });
    expect(within(list).getByText("disabled (by choice)")).toBeInTheDocument();
    expect(within(list).getByText("failing")).toBeInTheDocument();
    expect(await screen.findByRole("list", { name: "Catalog" })).toHaveTextContent("Receive PR events");
    expect(screen.getAllByRole("link", { name: "Add integration" }).length).toBeGreaterThan(0);
  });

  it("shows an empty state, and warns when stored credentials are unavailable", async () => {
    mockApi({ "GET /ready": readyOk, "GET /governance/me": me(["integration.view"]), "GET /integrations/installations": page([]), "GET /integrations/health": health({ secrets: { configured: false, reason: "No key", stored_credentials_available: false } }), "GET /integrations/catalog": catalog });
    renderApp("/integrations");
    expect(await screen.findByText("No integrations installed")).toBeInTheDocument();
    expect(screen.getByText(/Stored credentials are unavailable/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Add integration" })).not.toBeInTheDocument(); // viewers cannot add
  });

  it("renders a permission-denied state instead of data on 403", async () => {
    mockApi({ "GET /ready": readyOk, "GET /governance/me": me([]), "GET /integrations/installations": err(403, "forbidden", "no"), "GET /integrations/health": err(403, "forbidden", "no"), "GET /integrations/catalog": err(403, "forbidden", "no") });
    renderApp("/integrations");
    expect((await screen.findAllByTestId("permission-denied")).length).toBeGreaterThan(0);
  });

  it("creates an installation, sends the credential only in the body, shows the webhook secret once and never again", async () => {
    const { calls, fn } = mockApi({
      "GET /ready": readyOk, "GET /governance/me": me(["*"]), "GET /integrations/catalog": catalog,
      "GET /repositories": page([{ id: "r1", name: "my-repo" }]),
      "POST /integrations/installations": () => ({ status: 201, body: inst({ webhook_secret: "whsec_ONLYONCE123" }) }),
      "GET /integrations/installations": page([]), "GET /integrations/health": health(),
    });
    renderApp("/integrations/new");
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText("Integration type"), "github");
    await user.type(screen.getByLabelText("Name"), "GitHub main");
    await user.type(screen.getByLabelText("External repository"), "acme/widgets");
    await user.selectOptions(await screen.findByLabelText("NOVA repository"), "r1");
    await user.click(screen.getByLabelText("Store encrypted in NOVA"));
    const token = screen.getByLabelText("Token");
    expect(token).toHaveAttribute("type", "password");
    await user.type(token, "ghp_typed_value");
    await user.click(screen.getByRole("button", { name: "Create integration" }));
    const secret = await screen.findByTestId("one-time-secret");
    expect(secret).not.toHaveTextContent("whsec_ONLYONCE123"); // masked until revealed
    await user.click(within(secret).getByRole("button", { name: "Reveal" }));
    expect(secret).toHaveTextContent("whsec_ONLYONCE123");
    const post = calls.find((c) => c.key === "POST /integrations/installations");
    expect(post?.body).toMatchObject({ integration_type: "github", name: "GitHub main", credential: "ghp_typed_value", config: { repositories: [{ external: "acme/widgets", repository_id: "r1" }], publish_status: false } });
    expect(fn.mock.calls.every(([u]) => !String(u).includes("ghp_typed_value") && !String(u).includes("whsec_"))).toBe(true); // never in a URL
    await user.click(within(secret).getByRole("button", { name: "I have saved it" }));
    expect(screen.queryByTestId("one-time-secret")).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain("whsec_ONLYONCE123");
    expect(document.body.textContent).not.toContain("ghp_typed_value");
  });

  it("detail: tests the connection, requires confirmation to enable/revoke and never shows a stored credential", async () => {
    let enabled = false;
    const { calls } = mockApi({
      "GET /ready": readyOk, "GET /governance/me": me(["*"]),
      "GET /integrations/installations/i1": () => ({ body: inst({ enabled, health: enabled ? "unknown" : "disabled" }) }),
      "GET /integrations/installations/i1/events": page([{ id: "e1", delivery_id: "d-1", event_type: "check_run", action: "completed", status: "processed", external_ref: "1", repository_id: "r1", error_category: null, received_at: "2026-10-21T09:00:00Z", processed_at: null }]),
      "POST /integrations/installations/i1/test": { body: { ok: false, category: "auth_failed", detail: {} } },
      "POST /integrations/installations/i1/enable": () => { enabled = true; return { body: inst({ enabled: true }) }; },
      "POST /integrations/installations/i1/revoke": { body: { revoked: true } },
      "GET /integrations/installations": page([]), "GET /integrations/health": health(),
    });
    renderApp("/integrations/i1");
    const user = userEvent.setup();
    expect(await screen.findByRole("heading", { name: "GitHub main" })).toBeInTheDocument();
    expect(screen.getByText("stored (encrypted, hidden)")).toBeInTheDocument();
    expect(await screen.findByRole("list", { name: "Inbound events" })).toHaveTextContent("check_run");
    await user.click(screen.getByRole("button", { name: "Test connection" }));
    expect(await screen.findByText("Connection test failed: auth_failed.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Enable" }));
    const dlg = await screen.findByRole("dialog");
    expect(calls.some((c) => c.key === "POST /integrations/installations/i1/enable")).toBe(false); // nothing until confirmed
    await user.click(within(dlg).getByRole("button", { name: "Enable" }));
    await waitFor(() => expect(calls.some((c) => c.key === "POST /integrations/installations/i1/enable")).toBe(true));
    await user.click(await screen.findByRole("button", { name: /Revoke/ }));
    const dlg2 = await screen.findByRole("dialog");
    expect(within(dlg2).getByText(/audit history are kept/)).toBeInTheDocument();
    await user.click(within(dlg2).getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(calls.some((c) => c.key === "POST /integrations/installations/i1/revoke")).toBe(true));
  });

  it("detail hides management controls from users without integration.manage", async () => {
    mockApi({ "GET /ready": readyOk, "GET /governance/me": me(["integration.view"]), "GET /integrations/installations/i1": { body: inst() }, "GET /integrations/installations/i1/events": page([]) });
    renderApp("/integrations/i1");
    expect(await screen.findByRole("heading", { name: "GitHub main" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Test connection" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Revoke/ })).not.toBeInTheDocument();
  });

  it("CI evidence: unconfigured state, then runs where unknown is labelled as not a pass", async () => {
    mockApi({ "GET /ready": readyOk, "GET /integrations/ci-evidence": { body: { items: [], total: 0, page: 1, page_size: 20, pages: 1, configured: false } } });
    const first = renderApp("/integrations/ci-evidence");
    expect(await screen.findByText(/No integration is configured/)).toBeInTheDocument();
    first.unmount();
    mockApi({ "GET /ready": readyOk, "GET /integrations/ci-evidence": { body: { configured: true, items: [
      { id: "c1", installation_id: "i1", repository_id: "r1", source: "github", external_run_id: "9", name: "build", head_sha: "a".repeat(40), status: "unknown", external_url: "https://ci.example.com/9", started_at: null, completed_at: null, received_at: "2026-10-21T09:00:00Z" },
    ], total: 1, page: 1, page_size: 20, pages: 1 } } });
    renderApp("/integrations/ci-evidence");
    expect(await screen.findByText("unknown (not a pass)")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open in CI" })).toHaveAttribute("rel", "noreferrer noopener");
  });

  it("webhooks: lists deliveries, confirms replay and states at-least-once honestly", async () => {
    const { calls } = mockApi({
      "GET /ready": readyOk, "GET /integrations/webhook-subscriptions": page([{ id: "s1", name: "Ops hook", url: "https://hooks.example.com/x", events: ["workflow.created"], active: true, disabled_reason: null, consecutive_failures: 0, last_delivery_at: null, created_at: "2026-10-21T09:00:00Z", has_secret: true }]),
      "GET /integrations/catalog": catalog,
      "GET /integrations/webhook-deliveries": page([{ id: "d1", subscription_id: "s1", event_id: "e1", event_type: "workflow.created", state: "failed", attempts: 6, next_attempt_at: "2026-10-21T09:00:00Z", last_status_code: 503, last_error_category: "http_503", last_attempt_at: "2026-10-21T09:05:00Z", completed_at: null, replayed_by: null, created_at: "2026-10-21T09:00:00Z" }]),
      "POST /integrations/webhook-deliveries/d1/replay": { body: { id: "d1" } },
    });
    renderApp("/integrations/webhooks");
    const list = await screen.findByRole("list", { name: "Deliveries" });
    expect(list).toHaveTextContent("HTTP 503");
    expect(screen.getAllByText(/at-least-once/).length).toBeGreaterThan(0);
    const user = userEvent.setup();
    await user.click(within(list).getByRole("button", { name: "Replay" }));
    expect(calls.some((c) => c.key === "POST /integrations/webhook-deliveries/d1/replay")).toBe(false);
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Replay" }));
    await waitFor(() => expect(calls.some((c) => c.key === "POST /integrations/webhook-deliveries/d1/replay")).toBe(true));
  });

  it("extensions: built-in only, with a confirmation before enabling", async () => {
    const { calls } = mockApi({
      "GET /ready": readyOk, "GET /governance/me": me(["*"]),
      "GET /integrations/extensions": { body: { external_code_supported: false, items: [{ id: "github", name: "GitHub", version: "0.16.0", category: "version_control", nova_compatibility: ">=0.16.0", capabilities: [], permissions: ["read:events"], provenance: "built-in", compatible: true, status: "available", trusted: true }] } },
      "POST /integrations/extensions/github/enable": { body: { id: "github", status: "enabled" } },
    });
    renderApp("/integrations/extensions");
    expect(await screen.findByText(/third-party or uploaded code is not supported/)).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Enable" }));
    expect(calls.some((c) => c.key.startsWith("POST"))).toBe(false);
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Enable" }));
    await waitFor(() => expect(calls.some((c) => c.key === "POST /integrations/extensions/github/enable")).toBe(true));
  });
});
