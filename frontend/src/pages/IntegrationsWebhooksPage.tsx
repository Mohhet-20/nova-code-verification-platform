import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Badge, Button, Card, ConfirmDialog, EmptyState, Field, Notice, PageHeader, Pagination, TextInput } from "../components/ui";
import { describeError } from "../services/api";
import { formatDate } from "../services/format";
import { intApi } from "../services/integrationsApi";
import type { IntDelivery } from "../types/integrations";
import { Guard } from "./GovernanceShared";
import { AT_LEAST_ONCE, IntegrationsNav, OneTimeSecret } from "./IntegrationsShared";

const TONE: Record<string, "success" | "danger" | "warning" | "info" | "neutral"> = { succeeded: "success", failed: "danger", retrying: "warning", pending: "info", in_progress: "info" };

export function IntegrationWebhooksPage() {
  const qc = useQueryClient();
  const subs = useQuery({ queryKey: ["int-subs"], queryFn: intApi.subscriptions });
  const catalog = useQuery({ queryKey: ["int-catalog"], queryFn: intApi.catalog });
  const [state, setState] = useState("");
  const [page, setPage] = useState(1);
  const deliveries = useQuery({ queryKey: ["int-deliveries", state, page], queryFn: () => intApi.deliveries({ state: state || undefined, page }), placeholderData: (p) => p, enabled: subs.isSuccess });
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [events, setEvents] = useState<string[]>([]);
  const [secret, setSecret] = useState<{ label: string; value: string } | null>(null);
  const [replay, setReplay] = useState<IntDelivery | null>(null);
  const [detail, setDetail] = useState<string | null>(null);
  const refresh = () => { void qc.invalidateQueries({ queryKey: ["int-subs"] }); void qc.invalidateQueries({ queryKey: ["int-deliveries"] }); };
  const createM = useMutation({ mutationFn: () => intApi.createSubscription({ name: name.trim(), url: url.trim(), events }), onSuccess: (r) => { setSecret({ label: `Signing secret for ${r.name}`, value: r.secret ?? "" }); setName(""); setUrl(""); setEvents([]); refresh(); } });
  const activeM = useMutation({ mutationFn: (v: { id: string; active: boolean }) => intApi.setSubscriptionActive(v.id, v.active), onSuccess: refresh });
  const rotateM = useMutation({ mutationFn: (id: string) => intApi.rotateSubscriptionSecret(id), onSuccess: (r) => setSecret({ label: "New signing secret", value: r.secret }) });
  const replayM = useMutation({ mutationFn: (id: string) => intApi.replay(id), onSuccess: () => { setReplay(null); refresh(); } });
  const detailQ = useQuery({ queryKey: ["int-delivery", detail], queryFn: () => intApi.delivery(detail ?? ""), enabled: !!detail });
  return (
    <>
      <IntegrationsNav />
      <PageHeader title="Outbound webhooks" subtitle={`Signed event notifications to your own HTTPS endpoints. Payloads carry only a resource reference. ${AT_LEAST_ONCE}`} />
      <Guard query={subs} what="webhook subscriptions">
        {(d) => (
          <div className="space-y-6">
            {secret && <OneTimeSecret label={secret.label} value={secret.value} onDismiss={() => setSecret(null)} />}
            <Card title="Subscriptions">
              {d.items.length === 0 ? <EmptyState title="No subscriptions">Create one to start receiving signed events.</EmptyState> : (
                <ul className="divide-y divide-slate-100" aria-label="Subscriptions">
                  {d.items.map((s) => (
                    <li key={s.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                      <div><p className="font-medium">{s.name}</p><p className="text-xs text-slate-500">{s.url} · {s.events.join(", ")}{s.disabled_reason ? ` · disabled: ${s.disabled_reason}` : ""}</p></div>
                      <div className="flex items-center gap-2">
                        <Badge tone={s.active ? "success" : "neutral"}>{s.active ? "active" : "paused"}</Badge>
                        <Button variant="secondary" onClick={() => activeM.mutate({ id: s.id, active: !s.active })}>{s.active ? "Pause" : "Resume"}</Button>
                        <Button variant="secondary" onClick={() => rotateM.mutate(s.id)}>Rotate secret</Button>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
            <Card title="New subscription">
              <form className="max-w-xl space-y-3" aria-label="New subscription" onSubmit={(e) => { e.preventDefault(); createM.mutate(); }}>
                <Field label="Name" htmlFor="sub-name"><TextInput id="sub-name" required maxLength={120} value={name} onChange={(e) => setName(e.target.value)} /></Field>
                <Field label="Endpoint URL" htmlFor="sub-url" hint="HTTPS only. Private, loopback and cloud-metadata addresses are refused."><TextInput id="sub-url" required type="url" value={url} onChange={(e) => setUrl(e.target.value)} /></Field>
                <fieldset className="space-y-1"><legend className="text-sm font-medium">Events</legend>
                  {(catalog.data?.events ?? []).map((ev) => (
                    <label key={ev} className="flex items-center gap-2 text-sm"><input type="checkbox" checked={events.includes(ev)} onChange={(e) => setEvents(e.target.checked ? [...events, ev] : events.filter((x) => x !== ev))} />{ev}</label>
                  ))}
                </fieldset>
                {createM.isError && <Notice tone="danger">{describeError(createM.error)}</Notice>}
                <Button type="submit" disabled={createM.isPending || events.length === 0 || !name.trim() || !url.trim()}>Create subscription</Button>
              </form>
            </Card>
            <Card title="Deliveries">
              <div className="mb-3 max-w-xs">
                <Field label="State" htmlFor="dl-state">
                  <select id="dl-state" value={state} onChange={(e) => { setState(e.target.value); setPage(1); }} className="block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm">
                    <option value="">All</option>{["pending", "in_progress", "retrying", "succeeded", "failed"].map((s) => <option key={s}>{s}</option>)}
                  </select>
                </Field>
              </div>
              <Guard query={deliveries} what="deliveries">
                {(dl) => dl.items.length === 0 ? <EmptyState title="No deliveries">Events appear here once a subscribed event occurs.</EmptyState> : (
                  <>
                    <ul className="divide-y divide-slate-100" aria-label="Deliveries">
                      {dl.items.map((x) => (
                        <li key={x.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                          <div><p className="font-medium">{x.event_type}</p><p className="text-xs text-slate-500">attempts {x.attempts} · {x.last_status_code ? `HTTP ${x.last_status_code}` : "no response"}{x.last_error_category ? ` · ${x.last_error_category}` : ""} · {formatDate(x.last_attempt_at ?? x.created_at)}</p></div>
                          <div className="flex items-center gap-2">
                            <Badge tone={TONE[x.state] ?? "neutral"}>{x.state}</Badge>
                            <Button variant="ghost" onClick={() => setDetail(detail === x.id ? null : x.id)}>{detail === x.id ? "Hide attempts" : "Attempts"}</Button>
                            {(x.state === "failed" || x.state === "succeeded") && <Button variant="secondary" onClick={() => setReplay(x)}>Replay</Button>}
                          </div>
                        </li>
                      ))}
                    </ul>
                    <Pagination page={dl.page} pages={dl.pages} total={dl.total} onPage={setPage} />
                  </>
                )}
              </Guard>
              {detail && detailQ.data && (
                <ol className="mt-4 space-y-1 rounded-md bg-slate-50 p-3 text-xs" aria-label="Delivery attempts">
                  {detailQ.data.attempts_detail.map((a) => <li key={a.attempt_no}>#{a.attempt_no} · {a.status_code ?? "no response"} · {a.error_category ?? "ok"} · {a.duration_ms} ms{a.response_excerpt ? ` · ${a.response_excerpt}` : ""}</li>)}
                </ol>
              )}
            </Card>
          </div>
        )}
      </Guard>
      {replay && (
        <ConfirmDialog title="Replay this delivery?" confirmLabel="Replay" busy={replayM.isPending} error={replayM.isError ? describeError(replayM.error) : undefined} onConfirm={() => replayM.mutate(replay.id)} onCancel={() => setReplay(null)}>
          <p>The same event is sent again to the subscriber. {AT_LEAST_ONCE}</p>
        </ConfirmDialog>
      )}
    </>
  );
}
