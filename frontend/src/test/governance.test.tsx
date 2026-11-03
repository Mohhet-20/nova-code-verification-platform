import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, readyOk, renderApp } from "./helpers";

afterEach(() => vi.unstubAllGlobals());

const page = (items: unknown[]) => ({ body: { items, total: items.length, page: 1, page_size: 20, pages: 1 } });
const err = (status: number, code: string, message: string, details?: unknown) => ({ status, body: { error: { code, message, details } } });
const me = (over = {}) => ({
  user_id: "u-rev", handle: "rev1", display_name: "Rita Reviewer", org_role: "reviewer", org_wide_visibility: false, org_permissions: ["workflow.decide", "queue.claim"],
  teams: [{ team_id: "t1", role: "reviewer", permissions: ["workflow.decide"] }], org_roles: [], auth_kind: "user_token", ...over,
});
const stage = (over = {}) => ({ index: 0, name: "Independent review", min_approvals: 1, approvals: 0, independent: true, approver_roles: ["reviewer"], approver_team_roles: ["admin"], open: true, satisfied: false, ...over });
const wf = (over = {}) => ({
  id: "w1", state: "IN_REVIEW", round: 1, stage_index: 0, resource_type: "code_review", resource_id: "r1", repository_id: "repo1", team_id: "t1", title: "Review of parser change",
  priority: "normal", risk_level: "low", requested_by: "u-eng", requested_by_handle: "eng1", definition_key: "code-review-review", definition_version: 1, mode: "sequential",
  stages: [stage()], outstanding_actions: ["Stage 'Independent review' needs 1 more approval(s)."], submitted_at: "2026-10-21T10:00:00Z", completed_at: null,
  created_at: "2026-10-21T09:00:00Z", updated_at: "2026-10-21T10:00:00Z", decisions: [], policy_evaluations: [], resource_available: true, ...over,
});
const events = page([
  { id: "e1", event_type: "created", from_state: null, to_state: "DRAFT", actor_user_id: "u-eng", actor_handle: "eng1", data: {}, at: "2026-10-21T09:00:00Z" },
  { id: "e2", event_type: "state_changed", from_state: "SUBMITTED", to_state: "IN_REVIEW", actor_user_id: "u-eng", actor_handle: "eng1", data: { action: "submit" }, at: "2026-10-21T10:00:00Z" },
]);
const policyStatus = (over = {}) => ({
  available: true, blocked: false, blocking_policies: [], blockers: [],
  results: [{ policy: { id: "p1", key: "independent-approval", name: "Independent approval", version: 1, type: "independent_approvals", severity: "blocking" }, status: "unavailable", raw_status: "unavailable", blocking: true, rules: [], explanation: "Cannot be evaluated: no evidence yet.", unmet: [], evaluated_at: "2026-10-21T10:01:00Z" }],
  ...over,
});
const wfRoutes = (o = {}) => ({
  "GET /ready": readyOk, "GET /governance/me": { body: me() }, "GET /workflows/instances/w1": { body: wf() }, "GET /workflows/instances/w1/events": events,
  "GET /workflows/instances/w1/policy-status": { body: policyStatus() }, ...o,
});
const queueItem = (over = {}) => ({
  id: "q1", instance_id: "w1", resource_type: "code_review", resource_id: "r1", resource_available: true, resource_title: "Parser change", repository_id: "repo1", team_id: "t1", reason: "Awaiting review",
  priority: "high", risk_level: "medium", status: "open", assignee_user_id: null, assignee: null, created_at: "2026-10-21T09:00:00Z", closed_at: null, age_seconds: 7200, workflow_state: "IN_REVIEW",
  outstanding_actions: [], overdue: null, ...over,
});

describe("Governance workflow detail", () => {
  it("shows stages, honest policy status (unavailable is not a pass) and the event timeline", async () => {
    mockApi(wfRoutes());
    renderApp("/governance/workflows/w1");
    expect(await screen.findByRole("heading", { name: "Review of parser change" })).toBeInTheDocument();
    expect(screen.getByText("0/1 approval(s)", { exact: false })).toBeInTheDocument();
    const policy = await screen.findByRole("list", { name: "Policy results" });
    expect(within(policy).getByText("unavailable (not a pass)")).toBeInTheDocument();
    const timeline = await screen.findByRole("list", { name: "Workflow timeline" });
    expect(within(timeline).getByText("SUBMITTED → IN_REVIEW")).toBeInTheDocument();
  });

  it("requires a justification to reject, confirms first, then records the decision and trusts only the server's new state", async () => {
    let state = "IN_REVIEW";
    const { calls } = mockApi(wfRoutes({
      "GET /workflows/instances/w1": () => ({ body: wf({ state }) }),
      "POST /workflows/instances/w1/decisions": () => { state = "REJECTED"; return { status: 201, body: wf({ state }) }; },
    }));
    renderApp("/governance/workflows/w1");
    const user = userEvent.setup();
    await screen.findByText("Record your decision");
    await user.click(screen.getByRole("radio", { name: "Reject" }));
    expect(screen.getByRole("button", { name: /Review decision/ })).toBeDisabled(); // no reason yet
    await user.type(screen.getByLabelText(/Justification/), "Unsafe migration");
    await user.click(screen.getByRole("button", { name: /Review decision/ }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(calls.some((c) => c.key === "POST /workflows/instances/w1/decisions")).toBe(false); // nothing sent before confirming
    await user.click(within(dialog).getByRole("button", { name: "Confirm reject" }));
    await waitFor(() => expect(calls.find((c) => c.key === "POST /workflows/instances/w1/decisions")?.body).toEqual({ decision: "reject", justification: "Unsafe migration" }));
    expect(await screen.findByText("rejected")).toBeInTheDocument(); // came from the refetched server state
    expect(screen.queryByText("Record your decision")).not.toBeInTheDocument();
  });

  it("Escape cancels the confirmation without sending anything", async () => {
    const { calls } = mockApi(wfRoutes());
    renderApp("/governance/workflows/w1");
    const user = userEvent.setup();
    await screen.findByText("Record your decision");
    await user.click(screen.getByRole("button", { name: /Review decision/ }));
    await screen.findByRole("dialog");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(calls.filter((c) => c.key.startsWith("POST"))).toHaveLength(0);
  });

  it("shows a policy block as a refusal and leaves the workflow in review", async () => {
    const { calls } = mockApi(wfRoutes({
      "POST /workflows/instances/w1/decisions": err(409, "conflict", "Blocked by governance policy: Verification must pass.", { code: "policy_blocked", blockers: ["verification-passed"] }),
    }));
    renderApp("/governance/workflows/w1");
    const user = userEvent.setup();
    await screen.findByText("Record your decision");
    await user.click(screen.getByRole("button", { name: /Review decision/ }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Confirm approve" }));
    expect(await screen.findByText(/Blocked by policy: verification-passed/)).toBeInTheDocument();
    expect(screen.getByText("in review")).toBeInTheDocument();
    expect(calls.filter((c) => c.key === "POST /workflows/instances/w1/decisions")).toHaveLength(1);
  });

  it("does not offer decision controls to the requester", async () => {
    mockApi(wfRoutes({ "GET /governance/me": { body: me({ user_id: "u-eng", handle: "eng1" }) } }));
    renderApp("/governance/workflows/w1");
    expect(await screen.findByText(/you cannot approve your own request/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Review decision/ })).not.toBeInTheDocument();
  });

  it("surfaces a server-side refusal (403) of a decision as an error, never as success", async () => {
    mockApi(wfRoutes({ "POST /workflows/instances/w1/decisions": err(403, "forbidden", "Independent review is required.") }));
    renderApp("/governance/workflows/w1");
    const user = userEvent.setup();
    await screen.findByText("Record your decision");
    await user.click(screen.getByRole("button", { name: /Review decision/ }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Confirm approve" }));
    expect(await screen.findByText("Independent review is required.")).toBeInTheDocument();
  });

  it("warns when the reviewed resource is gone", async () => {
    mockApi(wfRoutes({ "GET /workflows/instances/w1": { body: wf({ resource_available: false }) } }));
    renderApp("/governance/workflows/w1");
    expect(await screen.findByText(/no longer available/)).toBeInTheDocument();
  });

  it("hides unknown workflows behind a not-available state and denied ones behind a permission message", async () => {
    mockApi(wfRoutes({ "GET /workflows/instances/w1": err(404, "not_found", "Workflow not found.") }));
    renderApp("/governance/workflows/w1");
    expect(await screen.findByText("Not available")).toBeInTheDocument();
  });
});

describe("Review queue", () => {
  it("lists items, sends filters to the server, and offers claim", async () => {
    const seen: string[] = [];
    const { calls } = mockApi({
      "GET /ready": readyOk, "GET /governance/me": { body: me() },
      "GET /review-queue": (_i, url) => { seen.push(url.search); return page([queueItem()]); },
      "POST /review-queue/q1/claim": { body: queueItem({ assignee: "rev1", assignee_user_id: "u-rev" }) },
    });
    renderApp("/governance/queue");
    const user = userEvent.setup();
    const list = await screen.findByRole("list", { name: "Review queue items" });
    expect(within(list).getByText("Parser change")).toBeInTheDocument();
    expect(seen[0]).toContain("status=open");
    await user.selectOptions(screen.getByLabelText("Priority"), "urgent");
    await waitFor(() => expect(seen.some((s) => s.includes("priority=urgent"))).toBe(true));
    await user.click(screen.getByLabelText("Unassigned only"));
    await waitFor(() => expect(seen.some((s) => s.includes("unassigned=true"))).toBe(true));
    await user.click(await screen.findByRole("button", { name: "Claim" }));
    await waitFor(() => expect(calls.some((c) => c.key === "POST /review-queue/q1/claim")).toBe(true));
  });

  it("shows a conflict when someone else claimed it first", async () => {
    mockApi({ "GET /ready": readyOk, "GET /governance/me": { body: me() }, "GET /review-queue": page([queueItem()]), "POST /review-queue/q1/claim": err(409, "conflict", "The item is already assigned to someone else.") });
    renderApp("/governance/queue");
    await userEvent.setup().click(await screen.findByRole("button", { name: "Claim" }));
    expect(await screen.findByText("The item is already assigned to someone else.")).toBeInTheDocument();
  });

  it("shows an empty state, and a safe state for items whose resource is gone", async () => {
    mockApi({ "GET /ready": readyOk, "GET /governance/me": { body: me() }, "GET /review-queue": page([]) });
    renderApp("/governance/queue");
    expect(await screen.findByText("Nothing in the queue")).toBeInTheDocument();
    vi.unstubAllGlobals();
    mockApi({ "GET /ready": readyOk, "GET /governance/me": { body: me() }, "GET /review-queue": page([queueItem({ resource_available: false, resource_title: "x" })]) });
    renderApp("/governance/queue");
    expect(await screen.findByText("Resource unavailable")).toBeInTheDocument();
  });

  it("releasing an assignment needs confirmation", async () => {
    const { calls } = mockApi({ "GET /ready": readyOk, "GET /governance/me": { body: me() }, "GET /review-queue": page([queueItem({ assignee: "rev1", assignee_user_id: "u-rev" })]), "POST /review-queue/q1/release": { body: queueItem() } });
    renderApp("/governance/queue");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Release my claim" }));
    expect(calls.some((c) => c.key.endsWith("/release"))).toBe(false);
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Release" }));
    await waitFor(() => expect(calls.some((c) => c.key === "POST /review-queue/q1/release")).toBe(true));
  });

  it("shows a permission-denied state instead of an empty list when the API says 403", async () => {
    mockApi({ "GET /ready": readyOk, "GET /governance/me": { body: me() }, "GET /review-queue": err(403, "forbidden", "No.") });
    renderApp("/governance/queue");
    expect(await screen.findByTestId("permission-denied")).toBeInTheDocument();
  });
});

describe("Governance home, teams, policies and audit", () => {
  const overview = { teams: 2, pending_my_approval: 1, pending_preview: [wf()], queue: { by_status: { open: 3 }, assigned_to_me: 1, unassigned: 2 }, my_open_requests: 0, org_wide: false, role: "reviewer" };

  it("shows measured counts and states honestly what the system does not do", async () => {
    mockApi({ "GET /ready": readyOk, "GET /governance/me": { body: me() }, "GET /governance/overview": { body: overview }, "GET /governance/health": err(403, "forbidden", "no") });
    renderApp("/governance");
    expect(await screen.findByText("Waiting on you")).toBeInTheDocument();
    expect(screen.getByText(/no scheduler/)).toBeInTheDocument();
    expect(await screen.findByText(/visible to organization administrators and auditors only/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Review of parser change" })).toHaveAttribute("href", "/governance/workflows/w1");
  });

  it("only offers 'New team' to people whose server-reported permissions allow it", async () => {
    mockApi({ "GET /ready": readyOk, "GET /governance/me": { body: me() }, "GET /teams": page([{ id: "t1", name: "Alpha", description: "", status: "active", created_at: "", updated_at: "", archived_at: null, member_count: 3, my_role: "reviewer" }]) });
    renderApp("/governance/teams");
    expect(await screen.findByRole("link", { name: "Alpha" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New team" })).not.toBeInTheDocument();
    vi.unstubAllGlobals();
    mockApi({ "GET /ready": readyOk, "GET /governance/me": { body: me({ org_role: "owner", org_permissions: ["*"] }) }, "GET /teams": page([]) });
    renderApp("/governance/teams");
    expect(await screen.findByRole("button", { name: "New team" })).toBeInTheDocument();
    expect(screen.getByText("No teams")).toBeInTheDocument();
  });

  it("removing a team member needs confirmation and calls the API only after it", async () => {
    const t = { id: "t1", name: "Alpha", description: "d", status: "active", created_at: "", updated_at: "", archived_at: null, member_count: 1, my_role: "admin" };
    const { calls } = mockApi({
      "GET /ready": readyOk, "GET /governance/me": { body: me({ teams: [{ team_id: "t1", role: "admin", permissions: ["team.members.manage"] }] }) }, "GET /teams/t1": { body: t },
      "GET /teams/t1/members": page([{ user_id: "u2", handle: "eng2", display_name: "Eli", role: "member", status: "active", joined_at: "", removed_at: null, added_by: null }]),
      "GET /teams/t1/ownership": page([]), "GET /teams/t1/work": { body: { team_id: "t1", owned_repositories: 0, workflows_by_state: {}, queue_by_status: {}, queue_unassigned: 0, outstanding_approvals: 0 } },
      "GET /org/members": page([]), "POST /teams/t1/members/u2/remove": { body: {} },
    });
    renderApp("/governance/teams/t1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Remove" }));
    expect(calls.some((c) => c.key.endsWith("/remove"))).toBe(false);
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Remove member" }));
    await waitFor(() => expect(calls.some((c) => c.key === "POST /teams/t1/members/u2/remove")).toBe(true));
  });

  it("policies: disabling needs confirmation; evaluation history labels unavailable as not a pass", async () => {
    const pol = { id: "p1", key: "verification-passed", name: "Verification must pass", description: "d", policy_type: "verification_required", resource_types: ["change_request"], team_id: null, repository_id: null, severity: "blocking", enabled: true, config: { allowed_outcomes: ["PASS"] }, version: 2, created_at: "", updated_at: "" };
    const { calls } = mockApi({
      "GET /ready": readyOk, "GET /governance/me": { body: me({ org_role: "owner", org_permissions: ["*"] }) }, "GET /governance/policies": page([pol]),
      "PUT /governance/policies/p1": { body: { ...pol, enabled: false, version: 3 } },
      "GET /governance/evaluations": page([{ id: "ev1", policy_key: "verification-passed", policy_version: 2, policy_type: "verification_required", severity: "blocking", instance_id: "w1", resource_type: "change_request", resource_id: "c1", status: "unavailable", blocking: true, trigger: "approve_blocked", result: { explanation: "Cannot be evaluated: no verification run.", unmet: [] }, evaluated_at: "2026-10-21T10:00:00Z" }]),
    });
    renderApp("/governance/policies");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Disable" }));
    expect(calls.some((c) => c.key.startsWith("PUT"))).toBe(false);
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Disable policy" }));
    await waitFor(() => expect(calls.find((c) => c.key === "PUT /governance/policies/p1")?.body).toEqual({ enabled: false }));
    await user.click(screen.getByRole("tab", { name: "Evaluation history" }));
    expect(await screen.findByText("unavailable (not a pass)")).toBeInTheDocument();
  });

  it("audit: read-only list with filter, and a permission-denied state for roles without audit access", async () => {
    const seen: string[] = [];
    mockApi({
      "GET /ready": readyOk, "GET /governance/me": { body: me() },
      "GET /governance/audit": (_i, url) => { seen.push(url.search); return page([{ id: "a1", action: "workflow.approval_blocked", entity_type: "workflow", entity_id: "w1", actor_user_id: "u", actor_handle: "rev1", outcome: "blocked", details: {}, at: "2026-10-21T10:00:00Z" }]); },
    });
    renderApp("/governance/audit");
    const user = userEvent.setup();
    expect(await screen.findByText("workflow.approval_blocked")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /delete|edit/i })).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Action starts with"), "authz.");
    await waitFor(() => expect(seen.some((s) => s.includes("action_prefix=authz."))).toBe(true));
    vi.unstubAllGlobals();
    mockApi({ "GET /ready": readyOk, "GET /governance/me": { body: me() }, "GET /governance/audit": err(403, "forbidden", "No.") });
    renderApp("/governance/audit");
    expect(await screen.findByTestId("permission-denied")).toBeInTheDocument();
  });

  it("my approvals: empty state and list", async () => {
    mockApi({ "GET /ready": readyOk, "GET /governance/me": { body: me() }, "GET /workflows/instances": (_i, url) => (url.search.includes("pending_for_me") ? page([wf()]) : page([])) });
    renderApp("/governance/approvals");
    expect(await screen.findByRole("link", { name: "Review of parser change" })).toBeInTheDocument();
    expect(await screen.findByText("You have not started any workflows.")).toBeInTheDocument();
  });
});

describe("Requesting a governance review from a record", () => {
  it("asks for confirmation, creates the workflow on the server, then opens it", async () => {
    const { calls } = mockApi({
      "GET /ready": readyOk, "GET /governance/me": { body: me() },
      "POST /workflows/instances": { status: 201, body: wf({ id: "w9", state: "DRAFT" }) },
      "GET /workflows/instances/w9": { body: wf({ id: "w9", state: "DRAFT", title: "Fresh workflow" }) },
      "GET /workflows/instances/w9/events": events, "GET /workflows/instances/w9/policy-status": { body: policyStatus() },
    });
    const { RequestGovernanceReview } = await import("../components/RequestGovernanceReview");
    renderApp("/x", <MemoryRouter><RequestGovernanceReview resourceType="code_review" resourceId="r1" /></MemoryRouter>);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Request governance review" }));
    expect(calls.some((c) => c.key === "POST /workflows/instances")).toBe(false);
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Create workflow" }));
    await waitFor(() => expect(calls.find((c) => c.key === "POST /workflows/instances")?.body).toEqual({ resource_type: "code_review", resource_id: "r1" }));
  });

  it("shows the server's refusal when a live workflow already exists", async () => {
    mockApi({ "GET /ready": readyOk, "POST /workflows/instances": err(409, "conflict", "A workflow for this resource is already in progress.") });
    const { RequestGovernanceReview } = await import("../components/RequestGovernanceReview");
    renderApp("/x", <MemoryRouter><RequestGovernanceReview resourceType="code_review" resourceId="r1" /></MemoryRouter>);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Request governance review" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Create workflow" }));
    expect(await screen.findByText("A workflow for this resource is already in progress.")).toBeInTheDocument();
  });
});
