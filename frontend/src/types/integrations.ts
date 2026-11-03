export type Health = "healthy" | "failing" | "disabled" | "unknown";
export type CiStatus = "pending" | "success" | "failed" | "cancelled" | "unknown";

export interface IntPage<T> { items: T[]; total: number; page: number; page_size: number; pages: number }

export interface IntCatalogItem {
  type: string; title: string; category: string; description: string; capabilities: string[]; permissions: string[];
  requires_credential: boolean; requires_webhook_secret: boolean;
}
export interface IntCatalog { items: IntCatalogItem[]; events: string[] }

export interface IntInstallation {
  id: string; integration_type: string; name: string; enabled: boolean; health: Health; connection_status: string; config: Record<string, unknown>;
  has_credential: boolean; credential_kind: "environment" | "stored" | null; requires_webhook_secret: boolean; has_webhook_secret: boolean; capabilities: string[];
  requested_permissions: string[]; granted_permissions: string[]; last_checked_at: string | null; last_success_at: string | null; last_sync_at: string | null;
  last_error_category: string | null; created_at: string; updated_at: string; inbound_path?: string | null;
  /** Present exactly once, in the create response. */
  webhook_secret?: string | null;
}

export interface IntTestResult { ok: boolean; category: string | null; detail: Record<string, unknown> }
export interface IntEvent { id: string; delivery_id: string; event_type: string; action: string; status: string; external_ref: string; repository_id: string | null; error_category: string | null; received_at: string; processed_at: string | null }
export interface IntHealth {
  installations: Record<string, number>; total: number; secrets: { configured: boolean; reason?: string; stored_credentials_available: boolean };
  outbound_queue: { pending: number; in_progress: number; retrying: number; failed: number; oldest_due_age_seconds: number | null; stuck: boolean };
  jobs: { queued: number; running: number; dead: number; oldest_due_age_seconds: number | null; stuck: boolean }; worker_enabled: boolean; private_destinations_allowed: boolean;
}
export interface IntSubscription { id: string; name: string; url: string; events: string[]; active: boolean; disabled_reason: string | null; consecutive_failures: number; last_delivery_at: string | null; created_at: string; has_secret: boolean; secret?: string }
export interface IntDelivery { id: string; subscription_id: string; event_id: string; event_type: string; state: string; attempts: number; next_attempt_at: string; last_status_code: number | null; last_error_category: string | null; last_attempt_at: string | null; completed_at: string | null; replayed_by: string | null; created_at: string }
export interface IntDeliveryDetail extends IntDelivery {
  envelope: Record<string, unknown> | null;
  attempts_detail: { attempt_no: number; started_at: string; duration_ms: number; status_code: number | null; error_category: string | null; response_excerpt: string }[];
}
export interface IntCiRun { id: string; installation_id: string; repository_id: string; source: string; external_run_id: string; name: string; head_sha: string; status: CiStatus; external_url: string | null; started_at: string | null; completed_at: string | null; received_at: string }
export interface IntCiEvidence extends IntPage<IntCiRun> { configured: boolean }
export interface IntExtension { id: string; name: string; version: string; category: string; nova_compatibility: string; capabilities: string[]; permissions: string[]; provenance: string; compatible: boolean; status: "enabled" | "available"; trusted: boolean }
export interface IntExtensions { items: IntExtension[]; external_code_supported: boolean }
export interface IntJob { id: string; operation: string; installation_id: string | null; state: string; attempts: number; max_attempts: number; scheduled_at: string; error_category: string | null; correlation_id: string }
export interface IntRepoMapping { external: string; repository_id: string }
