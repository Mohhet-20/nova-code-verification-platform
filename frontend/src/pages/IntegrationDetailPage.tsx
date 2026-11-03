import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Badge, Button, Card, ConfirmDialog, EmptyState, Field, Notice, PageHeader, Pagination, TextInput } from "../components/ui";
import { describeError } from "../services/api";
import { formatDate } from "../services/format";
import { intApi } from "../services/integrationsApi";
import type { IntInstallation, IntTestResult } from "../types/integrations";
import { Guard, can, useMe } from "./GovernanceShared";
import { HealthBadge, IntegrationsNav, OneTimeSecret } from "./IntegrationsShared";

type Confirm = "enable" | "disable" | "revoke" | "rotate-secret" | null;

export function IntegrationDetailPage() {
  const { installationId = "" } = useParams();
  const me = useMe();
  const manage = can(me.data, "integration.manage");
  const qc = useQueryClient();
  const nav = useNavigate();
  const q = useQuery({ queryKey: ["int-installation", installationId], queryFn: () => intApi.installation(installationId) });
  const [page, setPage] = useState(1);
  const events = useQuery({ queryKey: ["int-events", installationId, page], queryFn: () => intApi.events(installationId, page), placeholderData: (p) => p, enabled: q.isSuccess && q.data.requires_webhook_secret });
  const [confirm, setConfirm] = useState<Confirm>(null);
  const [test, setTest] = useState<IntTestResult | null>(null);
  const [secret, setSecret] = useState<string | null>(null);
  const [credential, setCredential] = useState("");
  const [credRef, setCredRef] = useState("");
  const refresh = () => { void qc.invalidateQueries({ queryKey: ["int-installation", installationId] }); void qc.invalidateQueries({ queryKey: ["int-installations"] }); void qc.invalidateQueries({ queryKey: ["int-health"] }); };
  const testM = useMutation({ mutationFn: () => intApi.test(installationId), onSuccess: (r) => { setTest(r); refresh(); } });
  const toggleM = useMutation({ mutationFn: (enable: boolean) => (enable ? intApi.enable(installationId) : intApi.disable(installationId)), onSuccess: () => { setConfirm(null); refresh(); } });
  const revokeM = useMutation({ mutationFn: () => intApi.revoke(installationId), onSuccess: () => { refresh(); nav("/integrations"); } });
  const rotateM = useMutation({ mutationFn: () => intApi.rotateWebhookSecret(installationId), onSuccess: (r) => { setConfirm(null); setSecret(r.webhook_secret); } });
  const credM = useMutation({
    mutationFn: () => intApi.replaceCredential(installationId, credRef.trim() ? { credential_ref: credRef.trim() } : { credential }),
    onSuccess: () => { setCredential(""); setCredRef(""); refresh(); },
  });
  const run = (i: IntInstallation) => (
    <div className="space-y-6">
      {secret && <OneTimeSecret label="New webhook signing secret" value={secret} onDismiss={() => setSecret(null)} />}
      <Card title="Status" actions={<HealthBadge health={i.health} />}>
        <dl className="grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
          <div><dt className="text-xs text-slate-500">Type</dt><dd>{i.integration_type}</dd></div>
          <div><dt className="text-xs text-slate-500">Last checked</dt><dd>{formatDate(i.last_checked_at)}</dd></div>
          <div><dt className="text-xs text-slate-500">Last success</dt><dd>{formatDate(i.last_success_at)}</dd></div>
          <div><dt className="text-xs text-slate-500">Last event</dt><dd>{formatDate(i.last_sync_at)}</dd></div>
          <div><dt className="text-xs text-slate-500">Credential</dt><dd>{i.has_credential ? (i.credential_kind === "stored" ? "stored (encrypted, hidden)" : "environment reference") : "none"}</dd></div>
          <div><dt className="text-xs text-slate-500">Last error</dt><dd>{i.last_error_category ?? "—"}</dd></div>
          <div className="col-span-2"><dt className="text-xs text-slate-500">Requested permissions</dt><dd className="flex flex-wrap gap-1">{i.requested_permissions.map((p) => <Badge key={p}>{p}</Badge>)}</dd></div>
        </dl>
        {i.inbound_path && <p className="mt-3 text-xs text-slate-600">Inbound webhook path: <code className="font-mono">{i.inbound_path}</code> (authenticated by signature; configure the same secret at the provider)</p>}
        {manage && (
          <div className="mt-4 flex flex-wrap gap-2">
            <Button variant="secondary" onClick={() => testM.mutate()} disabled={testM.isPending}>{testM.isPending ? "Testing…" : "Test connection"}</Button>
            <Button variant={i.enabled ? "secondary" : "primary"} onClick={() => setConfirm(i.enabled ? "disable" : "enable")}>{i.enabled ? "Disable" : "Enable"}</Button>
            {i.requires_webhook_secret && <Button variant="secondary" onClick={() => setConfirm("rotate-secret")}>Rotate webhook secret</Button>}
            <Button variant="danger" onClick={() => setConfirm("revoke")}>Revoke &amp; uninstall</Button>
          </div>
        )}
        {testM.isError && <div className="mt-3"><Notice tone="danger">{describeError(testM.error)}</Notice></div>}
        {test && <div className="mt-3"><Notice tone={test.ok ? "success" : "danger"}>{test.ok ? "Connection test succeeded." : `Connection test failed: ${test.category ?? "unknown"}.`}</Notice></div>}
      </Card>
      {manage && i.has_credential !== undefined && i.capabilities.includes("test_connection") && i.credential_kind && (
        <Card title="Replace credential">
          <form className="max-w-md space-y-3" aria-label="Replace credential" onSubmit={(e) => { e.preventDefault(); credM.mutate(); }}>
            <Field label="New token" htmlFor="rc-token" hint="Replaces the current credential. The old one is revoked and the new one is never shown again."><TextInput id="rc-token" type="password" autoComplete="off" value={credential} onChange={(e) => setCredential(e.target.value)} /></Field>
            <Field label="Or environment reference" htmlFor="rc-ref"><TextInput id="rc-ref" placeholder="env:NOVA_CRED_NAME" autoComplete="off" value={credRef} onChange={(e) => setCredRef(e.target.value)} /></Field>
            {credM.isError && <Notice tone="danger">{describeError(credM.error)}</Notice>}
            {credM.isSuccess && <Notice tone="success">Credential replaced. Run a connection test to verify it.</Notice>}
            <Button type="submit" disabled={credM.isPending || (!credential && !credRef.trim())}>Replace credential</Button>
          </form>
        </Card>
      )}
      {i.requires_webhook_secret && (
        <Card title="Inbound events">
          {events.data && events.data.items.length === 0 ? <EmptyState title="No events received">Nothing has been delivered to this installation yet.</EmptyState> : events.data ? (
            <>
              <ul className="divide-y divide-slate-100" aria-label="Inbound events">
                {events.data.items.map((e) => (
                  <li key={e.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                    <div><p className="font-medium">{e.event_type}{e.action ? ` · ${e.action}` : ""}</p><p className="text-xs text-slate-500">{e.delivery_id} · {formatDate(e.received_at)}{e.error_category ? ` · ${e.error_category}` : ""}</p></div>
                    <Badge tone={e.status === "processed" ? "success" : e.status === "failed" ? "danger" : "neutral"}>{e.status}</Badge>
                  </li>
                ))}
              </ul>
              <Pagination page={events.data.page} pages={events.data.pages} total={events.data.total} onPage={setPage} />
            </>
          ) : null}
        </Card>
      )}
      {confirm && (
        <ConfirmDialog
          title={{ enable: "Enable integration?", disable: "Disable integration?", revoke: "Revoke and uninstall?", "rotate-secret": "Rotate the webhook secret?" }[confirm]}
          confirmLabel={{ enable: "Enable", disable: "Disable", revoke: "Revoke", "rotate-secret": "Rotate" }[confirm]} tone={confirm === "revoke" || confirm === "rotate-secret" ? "danger" : "primary"}
          busy={toggleM.isPending || revokeM.isPending || rotateM.isPending}
          error={toggleM.isError ? describeError(toggleM.error) : revokeM.isError ? describeError(revokeM.error) : rotateM.isError ? describeError(rotateM.error) : undefined}
          onCancel={() => setConfirm(null)}
          onConfirm={() => (confirm === "enable" ? toggleM.mutate(true) : confirm === "disable" ? toggleM.mutate(false) : confirm === "revoke" ? revokeM.mutate() : rotateM.mutate())}>
          {confirm === "revoke" && <p>This disables the integration, revokes its credentials and stops accepting its webhooks. Recorded events, CI evidence and audit history are kept.</p>}
          {confirm === "rotate-secret" && <p>The current signing secret stops working immediately. Update the provider's webhook secret right after, or its deliveries will be rejected.</p>}
          {confirm === "enable" && <p>NOVA will start accepting signed events for this integration.</p>}
          {confirm === "disable" && <p>Incoming deliveries will be rejected until it is enabled again.</p>}
        </ConfirmDialog>
      )}
    </div>
  );
  return (
    <>
      <IntegrationsNav />
      <Guard query={q} what="this integration">
        {(i) => (<><PageHeader title={i.name} subtitle="Integration installation" actions={<Link className="text-sm text-sky-800 underline" to="/integrations">All integrations</Link>} />{run(i)}</>)}
      </Guard>
    </>
  );
}
