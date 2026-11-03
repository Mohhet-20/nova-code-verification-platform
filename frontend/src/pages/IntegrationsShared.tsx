import { useState } from "react";
import { NavLink } from "react-router-dom";
import { Badge, Button, Notice, cx } from "../components/ui";
import type { CiStatus, Health } from "../types/integrations";

const LINKS = [["/integrations", "Installations", true], ["/integrations/webhooks", "Outbound webhooks", false], ["/integrations/ci-evidence", "CI evidence", false], ["/integrations/extensions", "Extensions", false]] as const;

export function IntegrationsNav() {
  return (
    <nav aria-label="Integrations" className="mb-6 flex flex-wrap gap-1 border-b border-slate-200 pb-2">
      {LINKS.map(([to, label, end]) => (
        <NavLink key={to} to={to} end={end} className={({ isActive }) => cx("rounded-md px-3 py-1.5 text-sm font-medium", isActive ? "bg-sky-100 text-sky-900" : "text-slate-600 hover:bg-slate-100")}>
          {label}
        </NavLink>
      ))}
    </nav>
  );
}

const HEALTH: Record<Health, { tone: "success" | "danger" | "neutral" | "warning"; label: string }> = {
  healthy: { tone: "success", label: "healthy" }, failing: { tone: "danger", label: "failing" }, disabled: { tone: "neutral", label: "disabled (by choice)" }, unknown: { tone: "warning", label: "not checked yet" },
};
export const HealthBadge = ({ health }: { health: Health }) => <Badge tone={HEALTH[health].tone}>{HEALTH[health].label}</Badge>;

const CI: Record<CiStatus, "success" | "danger" | "neutral" | "warning" | "info"> = { success: "success", failed: "danger", pending: "info", cancelled: "neutral", unknown: "warning" };
export const CiBadge = ({ status }: { status: CiStatus }) => <Badge tone={CI[status]}>{status === "unknown" ? "unknown (not a pass)" : status}</Badge>;

/** A secret shown exactly once. It lives only in this component's props/state and disappears when dismissed; it is never put in a URL or storage. */
export function OneTimeSecret({ label, value, onDismiss }: { label: string; value: string; onDismiss: () => void }) {
  const [shown, setShown] = useState(false);
  return (
    <div role="status" className="space-y-2 rounded-md border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-900" data-testid="one-time-secret">
      <p className="font-semibold">{label}</p>
      <p>Copy this now. It is shown only once and cannot be retrieved again; if you lose it, rotate it.</p>
      <code className="block break-all rounded bg-white px-2 py-1 font-mono text-xs text-slate-900">{shown ? value : "•".repeat(24)}</code>
      <div className="flex gap-2">
        <Button variant="secondary" onClick={() => setShown((s) => !s)}>{shown ? "Hide" : "Reveal"}</Button>
        <Button variant="secondary" onClick={onDismiss}>I have saved it</Button>
      </div>
    </div>
  );
}

export const UNCONFIGURED = (
  <Notice tone="info">No integration is configured for this. Nothing here is estimated or assumed: until an installation sends or returns data, this page stays empty.</Notice>
);

export const AT_LEAST_ONCE = "Deliveries are at-least-once: a receiver may see the same event twice and should de-duplicate on the event id.";
