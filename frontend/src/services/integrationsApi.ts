import type {
  IntCatalog, IntCiEvidence, IntDelivery, IntDeliveryDetail, IntEvent, IntExtensions, IntHealth, IntInstallation, IntJob, IntPage, IntSubscription, IntTestResult,
} from "../types/integrations";
import { qs, request } from "./api";

const post = <T>(path: string, body?: unknown) => request<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) });
const put = <T>(path: string, body: unknown) => request<T>(path, { method: "PUT", body: JSON.stringify(body) });

/** Typed client for the integrations API. Secrets travel only in POST bodies, never in URLs or query strings, and are never returned by reads. */
export const intApi = {
  catalog: () => request<IntCatalog>("/integrations/catalog"),
  health: () => request<IntHealth>("/integrations/health"),
  installations: (page = 1) => request<IntPage<IntInstallation>>(`/integrations/installations${qs({ page, page_size: 50 })}`),
  installation: (id: string) => request<IntInstallation>(`/integrations/installations/${id}`),
  create: (body: { integration_type: string; name: string; config: Record<string, unknown>; credential?: string; credential_ref?: string }) => post<IntInstallation>("/integrations/installations", body),
  test: (id: string) => post<IntTestResult>(`/integrations/installations/${id}/test`),
  enable: (id: string) => post<IntInstallation>(`/integrations/installations/${id}/enable`),
  disable: (id: string) => post<IntInstallation>(`/integrations/installations/${id}/disable`),
  replaceCredential: (id: string, body: { credential?: string; credential_ref?: string }) => post<IntInstallation>(`/integrations/installations/${id}/credential`, body),
  rotateWebhookSecret: (id: string) => post<{ webhook_secret: string }>(`/integrations/installations/${id}/webhook-secret/rotate`),
  revoke: (id: string) => post<{ revoked: boolean }>(`/integrations/installations/${id}/revoke`),
  events: (id: string, page = 1) => request<IntPage<IntEvent>>(`/integrations/installations/${id}/events${qs({ page, page_size: 20 })}`),
  ciEvidence: (page = 1, status?: string) => request<IntCiEvidence>(`/integrations/ci-evidence${qs({ page, page_size: 20, status })}`),
  subscriptions: () => request<IntPage<IntSubscription>>(`/integrations/webhook-subscriptions${qs({ page_size: 100 })}`),
  createSubscription: (body: { name: string; url: string; events: string[] }) => post<IntSubscription>("/integrations/webhook-subscriptions", body),
  setSubscriptionActive: (id: string, active: boolean) => put<IntSubscription>(`/integrations/webhook-subscriptions/${id}`, { active }),
  rotateSubscriptionSecret: (id: string) => post<{ secret: string }>(`/integrations/webhook-subscriptions/${id}/rotate-secret`),
  removeSubscription: (id: string) => post<{ removed: boolean }>(`/integrations/webhook-subscriptions/${id}/remove`),
  deliveries: (p: { state?: string; page?: number }) => request<IntPage<IntDelivery>>(`/integrations/webhook-deliveries${qs({ state: p.state, page: p.page ?? 1, page_size: 20 })}`),
  delivery: (id: string) => request<IntDeliveryDetail>(`/integrations/webhook-deliveries/${id}`),
  replay: (id: string) => post<IntDelivery>(`/integrations/webhook-deliveries/${id}/replay`),
  runWorker: () => post<Record<string, unknown>>("/integrations/worker/run"),
  jobs: (page = 1) => request<IntPage<IntJob>>(`/integrations/jobs${qs({ page, page_size: 10 })}`),
  extensions: () => request<IntExtensions>("/integrations/extensions"),
  setExtension: (id: string, enabled: boolean) => post<{ id: string; status: string }>(`/integrations/extensions/${id}/${enabled ? "enable" : "disable"}`),
};
