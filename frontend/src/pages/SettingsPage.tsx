import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Button, Card, ErrorState, Field, Notice, PageHeader, Spinner, TextInput } from "../components/ui";
import { api, getStoredToken, setStoredToken } from "../services/api";

export function SettingsPage() {
  const ws = useQuery({ queryKey: ["workspace"], queryFn: api.workspace, retry: false });
  const [token, setToken] = useState(getStoredToken());
  const [tokenSaved, setTokenSaved] = useState(false);

  return (
    <>
      <PageHeader title="Settings & help" subtitle="Read-only view of the backend configuration, plus a reference for this version." />
      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Workspace & configuration">
          {ws.isPending && <Spinner />}
          {ws.isError && <ErrorState error={ws.error} onRetry={() => void ws.refetch()} />}
          {ws.isSuccess && (
            <dl className="space-y-2 text-sm">
              {[
                ["Workspace", ws.data.workspace_name],
                ["Identity", `${ws.data.user_display_name} (${ws.data.user_handle})`],
                ["Role", ws.data.role],
                ["Authentication", ws.data.auth_mode === "bearer-token" ? "Bearer token required" : "Local trust (no token)"],
                ["Multi-user isolation", ws.data.multi_user_isolation ? "Yes" : "Not implemented"],
                ["Planner", ws.data.planner],
                ["AI provider", ws.data.ai_provider === "none" ? "None (disabled)" : ws.data.ai_provider],
                ["Registration restricted", ws.data.registration_restricted ? "Yes (allow-listed directories)" : "No"],
                ["Backend version", ws.data.version],
              ].map(([k, v]) => (
                <div key={k} className="flex justify-between gap-4"><dt className="text-slate-500">{k}</dt><dd className="text-right">{v}</dd></div>
              ))}
              <div className="border-t border-slate-100 pt-2">
                <dt className="text-slate-500">Indexing limits</dt>
                <dd className="mt-1 font-mono text-xs">{Object.entries(ws.data.limits).map(([k, v]) => `${k}=${v.toLocaleString()}`).join("  ")}</dd>
              </div>
            </dl>
          )}
        </Card>
        <Card title="API access token">
          <p className="mb-3 text-sm text-slate-600">
            Only needed if the backend sets <code>NOVA_API_TOKEN</code>. The token is kept in this tab's session storage
            and sent only to the NOVA API.
          </p>
          <Field label="Bearer token" htmlFor="api-token">
            <TextInput id="api-token" type="password" autoComplete="off" value={token} onChange={(e) => { setToken(e.target.value); setTokenSaved(false); }} />
          </Field>
          <div className="mt-3 flex items-center gap-3">
            <Button onClick={() => { setStoredToken(token.trim()); setTokenSaved(true); void ws.refetch(); }}>Save token</Button>
            {tokenSaved && <span role="status" className="text-sm text-emerald-700">Saved for this tab.</span>}
          </div>
        </Card>
        <Card title="What this version does" className="lg:col-span-2">
          <div className="grid gap-4 text-sm md:grid-cols-2">
            <div>
              <h3 className="mb-1 font-semibold">Implemented</h3>
              <ul className="list-disc space-y-0.5 pl-5 text-slate-700">
                <li>Repository registration, bounded indexing, symbols, imports, call graph and impact analysis</li>
                <li>Static analysis findings, security and supply-chain scans, code review of local Git changes</li>
                <li>Test discovery and generation, failure investigation, repair proposals and refactoring plans</li>
                <li>Continuous verification, CI/CD readiness, governance workflows, integrations and operations views</li>
              </ul>
            </div>
            <div>
              <h3 className="mb-1 font-semibold">What can change or run</h3>
              <ul className="list-disc space-y-0.5 pl-5 text-slate-700">
                <li>Repository tests run only after you approve them, and only if the server allows it (<code>NOVA_TEST_EXECUTION_ENABLED</code>). They run as the server&apos;s own user: this is not a sandbox.</li>
                <li>Repository files change only when you explicitly apply an approved repair or refactoring.</li>
                <li>Data leaves this server only through integrations and webhooks an administrator configured, and, when the server enables Claude (<code>NOVA_AI_PROVIDER=anthropic</code>), the files of an investigation a user asks Claude to propose a fix for (secrets scrubbed).</li>
              </ul>
            </div>
          </div>
          <div className="mt-4"><Notice tone="info">Repository contents are treated as untrusted data. Register only repositories you trust if test execution is enabled.</Notice></div>
        </Card>
      </div>
    </>
  );
}
