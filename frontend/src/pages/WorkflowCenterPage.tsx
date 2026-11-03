import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Badge, Button, Card, EmptyState, ErrorState, Field, Notice, PageHeader, Pagination, Spinner, Stat, TextInput } from "../components/ui";
import { api } from "../services/api";
import { formatDate } from "../services/format";
import type { CiConnection } from "../types/api";
import { MutationError } from "./RepairsPage";
import { shortSha } from "./VerificationShared";
import { ReadinessBadge } from "./WorkflowShared";

function ConnectionRow({ repositoryId, c, canAdmin }: { repositoryId: string; c: CiConnection; canAdmin: boolean }) {
  const qc = useQueryClient();
  const refresh = () => { void qc.invalidateQueries({ queryKey: ["ci-overview", repositoryId] }); void qc.invalidateQueries({ queryKey: ["ci-crs", repositoryId] }); };
  const validate = useMutation({ mutationFn: () => api.ciValidate(repositoryId, c.id), onSuccess: refresh });
  const sync = useMutation({ mutationFn: () => api.ciSync(repositoryId, c.id), onSuccess: refresh });
  const tone = c.status === "validated" ? "success" : c.status === "error" ? "danger" : "warning";
  return (
    <li className="space-y-1 py-3 text-sm" data-testid={`conn-${c.id}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{c.provider === "local" ? "Local Git" : `GitHub · ${c.external_repo}`}</span>
        <Badge tone={tone}>{c.status}</Badge>
        {c.provider === "github" && <Badge tone={c.token_configured ? "success" : "warning"}>{c.token_configured ? "API token present" : "No API token"}</Badge>}
        {c.provider === "github" && <Badge tone={c.webhook_secret_configured ? "success" : "warning"}>{c.webhook_secret_configured ? "Webhook secret present" : "No webhook secret"}</Badge>}
        <span className="text-xs text-slate-500">validated {c.last_validated_at ? formatDate(c.last_validated_at) : "never"}</span>
      </div>
      {c.provider === "github" && <p className="text-xs text-slate-600">Credentials are read from the environment variables <code>{c.token_env || "(none)"}</code> and <code>{c.webhook_secret_env || "(none)"}</code>. Secret values are never stored or shown.</p>}
      {c.last_error && <Notice tone="warning">{c.last_error}</Notice>}
      {canAdmin && (
        <div className="flex gap-2">
          <Button variant="secondary" disabled={validate.isPending} onClick={() => validate.mutate()}>{validate.isPending ? "Validating…" : "Validate"}</Button>
          {c.provider === "local" && <Button variant="secondary" disabled={sync.isPending} onClick={() => sync.mutate()}>{sync.isPending ? "Scanning…" : "Scan branches"}</Button>}
        </div>
      )}
      <MutationError error={validate.error ?? sync.error} />
    </li>
  );
}

function ConnectForm({ repositoryId }: { repositoryId: string }) {
  const qc = useQueryClient();
  const [provider, setProvider] = useState<"local" | "github">("local");
  const [repo, setRepo] = useState("");
  const [tokenEnv, setTokenEnv] = useState("NOVA_GITHUB_TOKEN");
  const [secretEnv, setSecretEnv] = useState("NOVA_GITHUB_WEBHOOK_SECRET");
  const [branch, setBranch] = useState("main");
  const save = useMutation({
    mutationFn: () => api.ciSaveConnection(repositoryId, provider === "local" ? { provider, default_branch: branch } : { provider, external_repo: repo, default_branch: branch, token_env: tokenEnv, webhook_secret_env: secretEnv }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["ci-overview", repositoryId] }); },
  });
  return (
    <Card title="Add or update a connection">
      <form className="grid gap-3 sm:grid-cols-2" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
        <Field label="Provider" htmlFor="c-provider">
          <select id="c-provider" value={provider} onChange={(e) => setProvider(e.target.value as "local" | "github")} className="w-full rounded border border-slate-300 p-1.5 text-sm">
            <option value="local">Local Git branches</option><option value="github">GitHub (webhooks + status checks)</option>
          </select>
        </Field>
        <Field label="Default (target) branch" htmlFor="c-branch"><TextInput id="c-branch" value={branch} onChange={(e) => setBranch(e.target.value)} /></Field>
        {provider === "github" && (
          <>
            <Field label="GitHub repository (owner/name)" htmlFor="c-repo"><TextInput id="c-repo" value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="acme/shop" /></Field>
            <Field label="API token — environment variable NAME" htmlFor="c-token" hint="Enter the variable's name, never the token itself."><TextInput id="c-token" value={tokenEnv} onChange={(e) => setTokenEnv(e.target.value)} /></Field>
            <Field label="Webhook secret — environment variable NAME" htmlFor="c-secret"><TextInput id="c-secret" value={secretEnv} onChange={(e) => setSecretEnv(e.target.value)} /></Field>
          </>
        )}
        <div className="sm:col-span-2"><Button type="submit" disabled={save.isPending}>{save.isPending ? "Saving…" : "Save connection"}</Button></div>
      </form>
      <p className="mt-2 text-xs text-slate-500">Saving never contacts the provider. A connection stays “unconfigured” until you validate it.</p>
      <MutationError error={save.error} />
    </Card>
  );
}

export function WorkflowCenterPage() {
  const { repositoryId = "" } = useParams();
  const [state, setState] = useState("open");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const overview = useQuery({ queryKey: ["ci-overview", repositoryId], queryFn: () => api.ciOverview(repositoryId) });
  const list = useQuery({ queryKey: ["ci-crs", repositoryId, state, q, page], queryFn: () => api.ciChangeRequests(repositoryId, { state: state || undefined, q: q || undefined, page }) });
  if (overview.isLoading) return <Spinner />;
  if (overview.error || !overview.data) return <ErrorState error={overview.error} onRetry={() => void overview.refetch()} />;
  const o = overview.data;
  return (
    <div className="space-y-4">
      <PageHeader title="Engineering Workflow Center" subtitle="Pull requests, revision-bound verification and release readiness" />
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Open change requests" value={o.change_requests.open} /><Stat label="Closed" value={o.change_requests.closed} />
        <Stat label="Connections" value={o.connections.length} /><Stat label="Readiness policy" value={`v${o.readiness_policy_version}`} hint={o.readiness_policy_is_default ? "built-in default" : "custom"} />
      </div>
      <Card title="Integrations">
        {o.connections.length === 0 ? <EmptyState title="No connections yet">Add the local Git provider to review branches, or GitHub to receive pull-request webhooks.</EmptyState> : (
          <ul className="divide-y divide-slate-100">{o.connections.map((c) => <ConnectionRow key={c.id} repositoryId={repositoryId} c={c} canAdmin={o.can_admin} />)}</ul>
        )}
      </Card>
      {o.can_admin ? <ConnectForm repositoryId={repositoryId} /> : <Notice>Only a workspace owner can change provider connections.</Notice>}
      <Card title="Change requests">
        <div className="mb-3 flex flex-wrap items-end gap-3">
          <Field label="State" htmlFor="f-state">
            <select id="f-state" value={state} onChange={(e) => { setState(e.target.value); setPage(1); }} className="rounded border border-slate-300 p-1.5 text-sm">
              <option value="open">open</option><option value="closed">closed</option><option value="">all</option>
            </select>
          </Field>
          <Field label="Search title or branch" htmlFor="f-q"><TextInput id="f-q" value={q} onChange={(e) => { setQ(e.target.value); setPage(1); }} /></Field>
        </div>
        {list.isLoading ? <Spinner /> : list.error ? <ErrorState error={list.error} /> : !list.data || list.data.items.length === 0 ? (
          <EmptyState title="No change requests">Scan local branches or wait for a pull-request webhook.</EmptyState>
        ) : (
          <>
            <ul className="divide-y divide-slate-100" aria-label="Change requests">
              {list.data.items.map((cr) => (
                <li key={cr.id} className="flex flex-wrap items-center gap-2 py-2 text-sm" data-testid={`cr-${cr.id}`}>
                  <Link className="font-medium text-brand-700 hover:underline" to={`/repositories/${repositoryId}/workflow/change-requests/${cr.id}`}>{cr.title || cr.source_branch}</Link>
                  <Badge>{cr.provider}</Badge><Badge tone={cr.state === "open" ? "info" : "neutral"}>{cr.state}</Badge>
                  <code className="text-xs">{cr.source_branch} → {cr.target_branch}</code><code className="text-xs">{shortSha(cr.head_sha)}</code>
                  <span className="text-xs text-slate-500">{cr.files_count} file(s)</span>
                  <ReadinessBadge outcome={cr.verification.readiness} stale={cr.verification.stale} />
                  {!cr.verification.current_revision_verified && !cr.verification.stale && <Badge>Not verified</Badge>}
                </li>
              ))}
            </ul>
            <Pagination page={list.data.page} pages={list.data.pages} total={list.data.total} onPage={setPage} />
          </>
        )}
      </Card>
    </div>
  );
}
