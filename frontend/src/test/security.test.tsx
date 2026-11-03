import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { repoDetail } from "./fixtures";
import { mockApi, readyOk, renderApp } from "./helpers";

afterEach(() => vi.unstubAllGlobals());

const BASE = "/repositories/r1";
const S = `${BASE}/security`;
const SEC = `${BASE}/security`;
const page = (items: unknown[]) => ({ body: { items, total: items.length, page: 1, page_size: 20, pages: 1 } });
const scan = (over = {}) => ({
  id: "s1", status: "completed", scan_types: ["dependencies", "code", "secrets"], options: {}, head_sha: "a".repeat(40), dirty: false, error: null, created_at: "2026-10-20T10:00:00Z", started_at: null, finished_at: "2026-10-20T10:00:05Z",
  inventory_digest: "d", source_digest: "x",
  summary: { state: "partial", incomplete_reasons: ["advisory matching: 1 component(s) could not be assessed"], headline: "3 open finding(s). The scan is PARTIAL.", open_by_severity: { high: 2, medium: 1 } },
  coverage: { inventory: { unsupported: [{ path: "yarn.lock", detail: "Yarn lockfile" }] } }, ...over,
});
const prio = { bucket: "P2", score: 71.2, model: "nova.priority/1", factors: ["severity high -> base 75", "reachability unknown -> +0 (observed; absence of a signal is never used to lower the score)"] };
const finding = (over = {}) => ({
  id: "f1", kind: "secret", rule_id: "SEC-SECRET-001", category: "secrets", title: "Possible secret: GitHub token", severity: "high", confidence: "high", evidence_quality: "syntactic", rel_path: "settings.py", line: 2, status: "open",
  priority: prio, component_key: null, first_seen_at: "2026-10-20T10:00:00Z", last_seen_at: "2026-10-20T10:00:00Z", match_status: null, preview: "TOKEN = [REDACTED]", ...over,
});
const detail = (over = {}) => ({ ...finding(), description: "A value with the shape of a GitHub token appears in the file.", remediation: "Revoke and rotate the credential.", references: [], resolution_reason: null, suppression_id: null,
  evidence: { assessment: "a value with this shape was found; NOVA did not test whether it is a live credential" }, events: [{ action: "detected", from_status: null, to_status: "open", reason: "", actor_user_id: null, created_at: "2026-10-20T10:00:00Z" }], ...over });
const providers = [{ name: "workspace-store", enabled: true, kind: "local", detail: "cached advisories" }, { name: "osv", enabled: false, kind: "network", detail: "disabled (NOVA_SECURITY_OSV_ENABLED)" }];
const overview = (over = {}) => ({
  latest_scan: scan(), latest_completed_scan: scan(), findings_by_status: { open: 3, resolved: 0, false_positive: 0, accepted_risk: 0, suppressed: 0 }, open_by_severity: { critical: 0, high: 2, medium: 1, low: 0, info: 0 },
  latest_evaluation: null, policy_version: 0, policy_is_default: true, providers, can_write: true, can_admin: true, notice: null, ...over,
});
const decision = (status: string) => ({
  policy_id: "nova.security-policy", policy_version: 0, status, explanation: status === "indeterminate" ? "INDETERMINATE: there is not enough evidence for a decision. The scan is partial" : "FAIL: a finding exceeds the threshold", evaluated_at: "2026-10-20T10:01:00Z",
  missing_data: status === "indeterminate" ? ["advisory data"] : [], skipped_checks: [], checks: [{ id: "scan_complete", status: status === "indeterminate" ? "indeterminate" : "pass", detail: "The scan is partial" }], causes: [],
  exceptions_applied: [], expired_exceptions: [], revision: { head_sha: "a".repeat(40), dirty: false }, meaning: "Decision support only.",
});
const base = (o = {}) => ({ "GET /ready": readyOk, [`GET ${BASE}`]: { body: repoDetail }, [`GET ${SEC}/overview`]: { body: overview() }, [`GET ${SEC}/findings`]: page([finding()]), [`GET ${SEC}/findings/f1`]: { body: detail() }, ...o });

describe("Security Intelligence", () => {
  it("shows a partial scan with its coverage gaps and never calls it safe", async () => {
    mockApi(base());
    renderApp(S);
    expect(await screen.findByText("Security Intelligence")).toBeInTheDocument();
    const card = await screen.findByTestId("scan-card");
    expect(within(card).getByText("PARTIAL scan")).toBeInTheDocument();
    expect(within(card).getByText(/could not be assessed/)).toBeInTheDocument();
    expect(screen.getByText(/best-effort and not exhaustive/)).toBeInTheDocument();
    expect(screen.getByText("Open high").nextSibling).toHaveTextContent("2");
    expect(screen.queryByText(/no vulnerabilities|secure|safe\b/i)).toBeNull();
  });

  it("explains that nothing has been scanned yet instead of showing zeros as success", async () => {
    mockApi(base({ [`GET ${SEC}/overview`]: { body: overview({ latest_scan: null, latest_completed_scan: null, notice: "No scan has been run yet." }) } }));
    renderApp(S);
    expect(await screen.findByText("No scan has been run yet.")).toBeInTheDocument();
    expect(screen.getByText("No scan has been run")).toBeInTheDocument();
  });

  it("starts a scan with the chosen types and recorded exclusions", async () => {
    const m = mockApi(base({ [`POST ${SEC}/scans`]: { status: 202, body: scan({ status: "queued", summary: {} }) } }));
    renderApp(S);
    await screen.findByTestId("scan-card");
    await userEvent.click(screen.getByLabelText("dependencies"));
    await userEvent.type(screen.getByLabelText(/Exclude paths/), "fixtures/**");
    await userEvent.click(screen.getByRole("button", { name: "Run scan" }));
    await waitFor(() => expect(m.calls.find((c) => c.key === `POST ${SEC}/scans`)?.body).toEqual({ scan_types: ["code", "secrets"], exclude_globs: ["fixtures/**"] }));
  });

  it("hides write controls for a read-only role", async () => {
    mockApi(base({ [`GET ${SEC}/overview`]: { body: overview({ can_write: false, can_admin: false }) } }));
    renderApp(S);
    await screen.findByTestId("scan-card");
    expect(screen.queryByRole("button", { name: "Run scan" })).toBeNull();
    expect(screen.getByText(/can read results but not start scans/)).toBeInTheDocument();
  });

  it("lists findings with redacted previews and requires a reason before triage", async () => {
    const m = mockApi(base({ [`POST ${SEC}/findings/f1/triage`]: { body: detail({ status: "false_positive" }) }, [`GET ${SEC}/remediations`]: { body: [] } }));
    renderApp(S);
    await userEvent.click(await screen.findByRole("tab", { name: "Findings" }));
    const row = await screen.findByTestId("finding-f1");
    expect(row).toHaveTextContent("TOKEN = [REDACTED]");
    await userEvent.click(row);
    const d = await screen.findByTestId("finding-detail");
    expect(within(d).getByText(/NOVA did not test whether it is a live credential/)).toBeInTheDocument();
    expect(within(d).getByText(/never stored or shown/)).toBeInTheDocument();
    const fp = within(d).getByRole("button", { name: "Mark false positive" });
    expect(fp).toBeDisabled();
    await userEvent.type(within(d).getByLabelText(/Reason/), "synthetic test fixture");
    expect(fp).toBeEnabled();
    await userEvent.click(fp);
    await waitFor(() => expect(m.calls.find((c) => c.key === `POST ${SEC}/findings/f1/triage`)?.body).toEqual({ status: "false_positive", reason: "synthetic test fixture" }));
  });

  it("shows why a fix is not offered and never applies anything itself", async () => {
    const cannot = { id: "m1", finding_id: "f1", kind: "code", status: "cannot_fix", investigation_id: null, patch_id: null, proposal: { supported: false, reason: "Secrets are never rewritten automatically: revoke and rotate the credential." } };
    mockApi(base({ [`GET ${SEC}/remediations`]: { body: [cannot] } }));
    renderApp(S);
    await userEvent.click(await screen.findByRole("tab", { name: "Findings" }));
    await userEvent.click(await screen.findByTestId("finding-f1"));
    const box = await screen.findByTestId("remediation");
    expect(within(box).getByText("cannot fix")).toBeInTheDocument();
    expect(within(box).getAllByText(/revoke and rotate/).length).toBeGreaterThan(0);
  });

  it("proposes a fix, shows caveats and the diff, and submits it to the repair workflow without applying", async () => {
    const proposed = { id: "m2", finding_id: "f1", kind: "dependency", status: "proposed", investigation_id: null, patch_id: null, diff_text: "-pyyaml==5.3\n+pyyaml==5.4",
      proposal: { supported: true, change: "pyyaml==5.3 -> pyyaml==5.4", caveats: ["NOVA does not verify that this change preserves behavior.", "Lockfiles are not modified."] } };
    let current: unknown[] = [];
    const m = mockApi(base({
      [`GET ${SEC}/remediations`]: () => ({ body: current }),
      [`POST ${SEC}/findings/f1/remediations`]: () => { current = [proposed]; return { status: 201, body: proposed }; },
      [`POST ${SEC}/remediations/m2/submit`]: () => { current = [{ ...proposed, status: "submitted", investigation_id: "i1", patch_id: "p1" }]; return { body: current[0] }; },
    }));
    renderApp(S);
    await userEvent.click(await screen.findByRole("tab", { name: "Findings" }));
    await userEvent.click(await screen.findByTestId("finding-f1"));
    await userEvent.click(await screen.findByRole("button", { name: "Propose a fix" }));
    const box = await screen.findByTestId("remediation");
    expect(await within(box).findByText(/does not verify that this change preserves behavior/)).toBeInTheDocument();
    expect(within(box).getByText(/\+pyyaml==5.4/)).toBeInTheDocument();
    await userEvent.click(within(box).getByRole("button", { name: /Create repair patch and validate/ }));
    expect(await within(box).findByText(/Nothing has been applied/)).toBeInTheDocument();
    expect(m.calls.some((c) => c.key.endsWith("/apply"))).toBe(false);
  });

  it("shows indeterminate as INDETERMINATE with its missing data, never as a pass", async () => {
    const m = mockApi(base({
      [`GET ${SEC}/policy`]: { body: { version: 0, is_default: true, rules: { fail_on_severity: "high" }, can_edit: true, priority_model: "nova.priority/1" } },
      [`GET ${SEC}/suppressions`]: { body: [] }, [`GET ${SEC}/exceptions`]: { body: [] }, [`POST ${SEC}/evaluate`]: { body: decision("indeterminate") },
    }));
    renderApp(S);
    await userEvent.click(await screen.findByRole("tab", { name: "Policy gate" }));
    await userEvent.click(await screen.findByRole("button", { name: "Evaluate latest scan" }));
    const ev = await screen.findByTestId("evaluation");
    expect(within(ev).getByText("INDETERMINATE")).toBeInTheDocument();
    expect(within(ev).queryByText("PASS")).toBeNull();
    expect(within(ev).getByText(/Missing data: advisory data/)).toBeInTheDocument();
    expect(m.calls.some((c) => c.key === `POST ${SEC}/evaluate`)).toBe(true);
  });

  it("lets only an owner edit the policy and validates JSON before saving", async () => {
    mockApi(base({
      [`GET ${SEC}/policy`]: { body: { version: 1, is_default: false, rules: { fail_on_severity: "high" }, can_edit: false, priority_model: "nova.priority/1" } },
      [`GET ${SEC}/suppressions`]: { body: [] }, [`GET ${SEC}/exceptions`]: { body: [] },
      [`GET ${SEC}/overview`]: { body: overview({ can_admin: false }) },
    }));
    renderApp(S);
    await userEvent.click(await screen.findByRole("tab", { name: "Policy gate" }));
    expect(await screen.findByText(/Only a workspace owner can change the security policy/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit policy" })).toBeNull();
  });

  it("shows inventory with unassessed components and unsupported manifests called out", async () => {
    const comps = { items: [{ id: "c1", ecosystem: "PyPI", name: "requests", version: null, version_state: "range", relationship: "direct", scope: "runtime", manifests: [{ path: "requirements.txt" }], declared: [], paths: [], license: null, notes: ["declared as '>=2.0'"], assessment: { status: "unassessed" } }], total: 1, page: 1, page_size: 25, pages: 1 };
    mockApi(base({ [`GET ${SEC}/scans/s1/components`]: { body: comps } }));
    renderApp(S);
    await userEvent.click(await screen.findByRole("tab", { name: "Inventory" }));
    const row = await screen.findByTestId("comp-requests");
    expect(row).toHaveTextContent("version unknown");
    expect(within(row).getByText("unassessed")).toBeInTheDocument();
    expect(screen.getByText(/yarn\.lock/)).toBeInTheDocument();
    expect(screen.getByText(/not a safety claim/)).toBeInTheDocument();
  });

  it("shows advisory sources, retrieval time and an unspecified severity honestly", async () => {
    const adv = detail({ id: "f9", kind: "dependency", title: "flask 2.0.0: SYN-0003", severity: "medium", match_status: "confirmed", preview: null,
      evidence: { reason: "installed version is in an affected range", severity_source: "unspecified", fixed_versions: ["2.0.1"], sources: [{ provider: "fixtures:advisories", advisory_id: "SYN-0003", url: null, retrieved_at: "2026-10-20T10:00:00Z" }] } });
    mockApi(base({ [`GET ${SEC}/advisory-matches`]: page([adv]) }));
    renderApp(S);
    await userEvent.click(await screen.findByRole("tab", { name: "Advisories" }));
    const item = await screen.findByTestId("adv-f9");
    expect(within(item).getByText("severity not provided by source")).toBeInTheDocument();
    expect(item).toHaveTextContent("SYN-0003 via fixtures:advisories");
    expect(item).toHaveTextContent("Fixed in: 2.0.1");
  });

  it("generates a report and downloads it", async () => {
    const createUrl = vi.fn(() => "blob:x");
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: createUrl, revokeObjectURL: vi.fn() }));
    const m = mockApi(base({
      [`POST ${SEC}/reports`]: { status: 201, body: { id: "rp1", scan_id: "s1", format: "markdown", sha256: "0".repeat(64), size_bytes: 10, truncated: false, created_at: "2026-10-20T10:00:00Z" } },
      [`GET ${SEC}/reports/rp1`]: { body: { id: "rp1", scan_id: "s1", format: "markdown", sha256: "0".repeat(64), size_bytes: 10, truncated: false, created_at: "2026-10-20T10:00:00Z", body: "# report" } },
    }));
    renderApp(S);
    await userEvent.click(await screen.findByRole("button", { name: "Report (markdown)" }));
    await waitFor(() => expect(createUrl).toHaveBeenCalled());
    expect(m.calls.find((c) => c.key === `POST ${SEC}/reports`)?.body).toEqual({ format: "markdown" });
  });
});
