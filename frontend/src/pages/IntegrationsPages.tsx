import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Badge, Button, ButtonLink, Card, EmptyState, Field, Notice, PageHeader, Pagination, Stat, TextInput, ConfirmDialog } from "../components/ui";
import { api, describeError } from "../services/api";
import { formatDate } from "../services/format";
import { intApi } from "../services/integrationsApi";
import type { IntCatalogItem, IntRepoMapping } from "../types/integrations";
import { Guard, can, useMe } from "./GovernanceShared";
import { CiBadge, HealthBadge, IntegrationsNav, OneTimeSecret, UNCONFIGURED } from "./IntegrationsShared";

export function IntegrationsHomePage() {
  const me = useMe();
  const manage = can(me.data, "integration.manage");
  const list = useQuery({ queryKey: ["int-installations"], queryFn: () => intApi.installations() });
  const health = useQuery({ queryKey: ["int-health"], queryFn: intApi.health });
  const catalog = useQuery({ queryKey: ["int-catalog"], queryFn: intApi.catalog });
  return (
    <>
      <IntegrationsNav />
      <PageHeader title="Integration Center" subtitle="Connect NOVA to your source host, CI and issue tracker. Integrations only supply evidence and references: policy and workflow decisions stay inside NOVA."
        actions={manage ? <ButtonLink to="/integrations/new">Add integration</ButtonLink> : undefined} />
      <Guard query={list} what="integrations">
        {(d) => (
          <div className="space-y-6">
            {health.data && (
              <>
                {!health.data.secrets.configured && (
                  <Notice tone="warning">Stored credentials are unavailable: no encryption key is configured on the server. You can still use environment credential references (NOVA_CRED_…). {health.data.secrets.reason}</Notice>
                )}
                <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                  <Stat label="Installations" value={health.data.total} />
                  <Stat label="Failing" value={health.data.installations.failing ?? 0} hint="Enabled but last check failed" />
                  <Stat label="Outbound queue" value={health.data.outbound_queue.pending + health.data.outbound_queue.retrying} hint={health.data.outbound_queue.stuck ? "Stuck: nothing is processing deliveries" : health.data.worker_enabled ? "Worker enabled" : "Worker not enabled"} />
                  <Stat label="Dead jobs" value={health.data.jobs.dead} />
                </div>
              </>
            )}
            {d.items.length === 0 ? (
              <EmptyState title="No integrations installed" action={manage ? <ButtonLink to="/integrations/new">Add integration</ButtonLink> : undefined}>
                Install one of the built-in integrations below to receive pull request and CI events. Nothing is connected by default.
              </EmptyState>
            ) : (
              <Card title="Installations">
                <ul className="divide-y divide-slate-100" aria-label="Installations">
                  {d.items.map((i) => (
                    <li key={i.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                      <div>
                        <Link className="font-medium text-sky-800 hover:underline" to={`/integrations/${i.id}`}>{i.name}</Link>
                        <p className="text-xs text-slate-500">{i.integration_type} · last checked {formatDate(i.last_checked_at)}{i.last_error_category ? ` · ${i.last_error_category}` : ""}</p>
                      </div>
                      <HealthBadge health={i.health} />
                    </li>
                  ))}
                </ul>
              </Card>
            )}
            <Guard query={catalog} what="the integration catalog">
              {(c) => (
                <Card title="Available integrations">
                  <ul className="grid gap-3 md:grid-cols-3" aria-label="Catalog">
                    {c.items.map((it) => (
                      <li key={it.type} className="rounded-md border border-slate-200 p-3 text-sm">
                        <p className="font-medium">{it.title}</p>
                        <p className="mt-1 text-xs text-slate-600">{it.description}</p>
                        <p className="mt-2 flex flex-wrap gap-1">{it.capabilities.map((cap) => <Badge key={cap}>{cap}</Badge>)}</p>
                      </li>
                    ))}
                  </ul>
                </Card>
              )}
            </Guard>
          </div>
        )}
      </Guard>
    </>
  );
}

export function IntegrationNewPage() {
  const nav = useNavigate();
  const qc = useQueryClient();
  const catalog = useQuery({ queryKey: ["int-catalog"], queryFn: intApi.catalog });
  const repos = useQuery({ queryKey: ["int-repos"], queryFn: () => api.listRepositories(1, 100) });
  const [type, setType] = useState("");
  const [name, setName] = useState("");
  const [external, setExternal] = useState("");
  const [repositoryId, setRepositoryId] = useState("");
  const [project, setProject] = useState("");
  const [optIn, setOptIn] = useState(false);
  const [credMode, setCredMode] = useState<"env" | "stored">("env");
  const [credRef, setCredRef] = useState("");
  const [credential, setCredential] = useState("");
  const [created, setCreated] = useState<{ id: string; secret: string | null } | null>(null);
  const item: IntCatalogItem | undefined = catalog.data?.items.find((i) => i.type === type);
  const m = useMutation({
    mutationFn: () => {
      const mapping: IntRepoMapping[] = external && repositoryId ? [{ external: external.trim(), repository_id: repositoryId }] : [];
      const config: Record<string, unknown> = type === "github-issues" ? { project: project.trim(), allow_create: optIn } : type === "github" ? { repositories: mapping, publish_status: optIn } : { repositories: mapping };
      return intApi.create({ integration_type: type, name: name.trim(), config, ...(item?.requires_credential ? (credMode === "env" ? { credential_ref: credRef.trim() } : { credential }) : {}) });
    },
    onSuccess: (r) => { setCredential(""); void qc.invalidateQueries({ queryKey: ["int-installations"] }); setCreated({ id: r.id, secret: r.webhook_secret ?? null }); },
  });
  const submit = (e: FormEvent) => { e.preventDefault(); m.mutate(); };
  if (created) {
    return (
      <>
        <IntegrationsNav />
        <PageHeader title="Integration created" subtitle="It is disabled until you test it and turn it on." />
        <div className="space-y-4">
          {created.secret && <OneTimeSecret label="Webhook signing secret" value={created.secret} onDismiss={() => setCreated({ ...created, secret: null })} />}
          <Button onClick={() => nav(`/integrations/${created.id}`)} disabled={created.secret !== null}>Continue to the integration</Button>
          {created.secret !== null && <p className="text-xs text-slate-500">Confirm you saved the secret to continue.</p>}
        </div>
      </>
    );
  }
  return (
    <>
      <IntegrationsNav />
      <PageHeader title="Add integration" subtitle="Credentials are sent once over a request body and are never shown again." />
      <Guard query={catalog} what="the integration catalog">
        {(c) => (
          <form onSubmit={submit} className="max-w-xl space-y-4" aria-label="New integration">
            <Field label="Integration type" htmlFor="int-type">
              <select id="int-type" required value={type} onChange={(e) => setType(e.target.value)} className="block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm">
                <option value="">Choose…</option>
                {c.items.map((i) => <option key={i.type} value={i.type}>{i.title}</option>)}
              </select>
            </Field>
            {item && <p className="text-xs text-slate-600">{item.description} Requests these permissions from the external system: {item.permissions.join(", ") || "none"}.</p>}
            <Field label="Name" htmlFor="int-name"><TextInput id="int-name" required maxLength={120} value={name} onChange={(e) => setName(e.target.value)} /></Field>
            {(type === "github" || type === "local-test") && (
              <>
                <Field label="External repository" htmlFor="int-ext" hint="owner/name as the provider spells it"><TextInput id="int-ext" required={type === "github"} value={external} onChange={(e) => setExternal(e.target.value)} /></Field>
                <Field label="NOVA repository" htmlFor="int-repo" hint="Events for the external repository are attached to this repository only.">
                  <select id="int-repo" required={type === "github"} value={repositoryId} onChange={(e) => setRepositoryId(e.target.value)} className="block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm">
                    <option value="">Choose…</option>
                    {(repos.data?.items ?? []).map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
                  </select>
                </Field>
              </>
            )}
            {type === "github-issues" && <Field label="Project" htmlFor="int-project" hint="owner/name"><TextInput id="int-project" required value={project} onChange={(e) => setProject(e.target.value)} /></Field>}
            {(type === "github" || type === "github-issues") && (
              <label className="flex items-start gap-2 text-sm">
                <input type="checkbox" checked={optIn} onChange={(e) => setOptIn(e.target.checked)} className="mt-1" />
                <span>{type === "github" ? "Allow NOVA to publish a commit status (requires write permission; off by default)" : "Allow creating issues from NOVA (explicit action by an administrator; off by default)"}</span>
              </label>
            )}
            {item?.requires_credential && (
              <fieldset className="space-y-2 rounded-md border border-slate-200 p-3">
                <legend className="px-1 text-sm font-medium">Credential</legend>
                <label className="flex items-center gap-2 text-sm"><input type="radio" name="cred-mode" checked={credMode === "env"} onChange={() => setCredMode("env")} /> Environment reference (recommended for local use)</label>
                <label className="flex items-center gap-2 text-sm"><input type="radio" name="cred-mode" checked={credMode === "stored"} onChange={() => setCredMode("stored")} /> Store encrypted in NOVA</label>
                {credMode === "env" ? (
                  <Field label="Environment reference" htmlFor="int-ref" hint="Must look like env:NOVA_CRED_NAME. The value is read from the server environment."><TextInput id="int-ref" required placeholder="env:NOVA_CRED_GITHUB" value={credRef} onChange={(e) => setCredRef(e.target.value)} autoComplete="off" /></Field>
                ) : (
                  <Field label="Token" htmlFor="int-cred" hint="Encrypted on the server. It cannot be viewed again, only replaced."><TextInput id="int-cred" type="password" required autoComplete="off" value={credential} onChange={(e) => setCredential(e.target.value)} /></Field>
                )}
              </fieldset>
            )}
            {m.isError && <Notice tone="danger">{describeError(m.error)}</Notice>}
            <Button type="submit" disabled={!type || !name.trim() || m.isPending}>{m.isPending ? "Creating…" : "Create integration"}</Button>
          </form>
        )}
      </Guard>
    </>
  );
}

export function IntegrationCiEvidencePage() {
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState("");
  const q = useQuery({ queryKey: ["int-ci", page, status], queryFn: () => intApi.ciEvidence(page, status || undefined), placeholderData: (p) => p });
  return (
    <>
      <IntegrationsNav />
      <PageHeader title="CI evidence" subtitle="External CI runs recorded for commits. Evidence informs policy; it never approves or releases anything by itself. Pending, unknown and stale runs do not count as a pass." />
      <div className="mb-4 max-w-xs">
        <Field label="Status" htmlFor="ci-status">
          <select id="ci-status" value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }} className="block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm">
            <option value="">All</option>{["pending", "success", "failed", "cancelled", "unknown"].map((s) => <option key={s}>{s}</option>)}
          </select>
        </Field>
      </div>
      <Guard query={q} what="CI evidence">
        {(d) => !d.configured ? UNCONFIGURED : d.items.length === 0 ? <EmptyState title="No CI runs recorded">An integration is installed but has not delivered any CI event yet.</EmptyState> : (
          <Card>
            <ul className="divide-y divide-slate-100" aria-label="CI runs">
              {d.items.map((r) => (
                <li key={r.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                  <div>
                    <p className="font-medium">{r.name || r.external_run_id} <span className="font-mono text-xs text-slate-500">{r.head_sha.slice(0, 10)}</span></p>
                    <p className="text-xs text-slate-500">{r.source} · run {r.external_run_id} · received {formatDate(r.received_at)}{r.completed_at ? ` · finished ${formatDate(r.completed_at)}` : ""}</p>
                  </div>
                  <div className="flex items-center gap-2">{r.external_url && <a className="text-xs text-sky-800 underline" href={r.external_url} rel="noreferrer noopener" target="_blank">Open in CI</a>}<CiBadge status={r.status} /></div>
                </li>
              ))}
            </ul>
            <Pagination page={d.page} pages={d.pages} total={d.total} onPage={setPage} />
          </Card>
        )}
      </Guard>
    </>
  );
}

export function IntegrationExtensionsPage() {
  const me = useMe();
  const manage = can(me.data, "extension.manage");
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["int-ext"], queryFn: intApi.extensions });
  const [pending, setPending] = useState<{ id: string; enable: boolean } | null>(null);
  const m = useMutation({ mutationFn: (v: { id: string; enable: boolean }) => intApi.setExtension(v.id, v.enable), onSuccess: () => { setPending(null); void qc.invalidateQueries({ queryKey: ["int-ext"] }); } });
  return (
    <>
      <IntegrationsNav />
      <PageHeader title="Extensions" subtitle="Only extensions that ship with NOVA are listed. Loading third-party or uploaded code is not supported in this release." />
      <Guard query={q} what="extensions">
        {(d) => (
          <Card>
            <ul className="divide-y divide-slate-100" aria-label="Extensions">
              {d.items.map((e) => (
                <li key={e.id} className="flex flex-wrap items-center justify-between gap-2 py-3 text-sm">
                  <div>
                    <p className="font-medium">{e.name} <span className="text-xs text-slate-500">v{e.version} · {e.category}</span></p>
                    <p className="text-xs text-slate-500">Provenance: {e.provenance} · compatible with NOVA {e.nova_compatibility}{e.compatible ? "" : " (not compatible with this version)"}</p>
                    <p className="mt-1 flex flex-wrap gap-1">{e.permissions.map((p) => <Badge key={p}>{p}</Badge>)}</p>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge tone={e.status === "enabled" ? "success" : "neutral"}>{e.status}</Badge>
                    {manage && <Button variant="secondary" onClick={() => setPending({ id: e.id, enable: e.status !== "enabled" })}>{e.status === "enabled" ? "Disable" : "Enable"}</Button>}
                  </div>
                </li>
              ))}
            </ul>
          </Card>
        )}
      </Guard>
      {pending && (
        <ConfirmDialog title={`${pending.enable ? "Enable" : "Disable"} extension?`} confirmLabel={pending.enable ? "Enable" : "Disable"} busy={m.isPending} error={m.isError ? describeError(m.error) : undefined}
          onConfirm={() => m.mutate(pending)} onCancel={() => setPending(null)}>
          <p>This changes what the {pending.id} extension may do for your organization. It cannot change approvals, policies or the audit log.</p>
        </ConfirmDialog>
      )}
    </>
  );
}
