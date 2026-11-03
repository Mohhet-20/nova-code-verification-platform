import type {
  OpsAlertEval,
  OpsDiagnostic,
  OpsHealth,
  OpsIncident,
  OpsIncidentDetail,
  OpsLogRecord,
  OpsOverview,
  OpsSignal,
  OpsSlo,
  OpsSpan,
  SecComponent,
  SecEvaluation,
  SecException,
  SecFinding,
  SecFindingDetail,
  SecOverview,
  SecPolicyView,
  SecRemediation,
  SecReportSummary,
  SecScan,
  SecSuppression,
  CiChangeRequest,
  CiConnection,
  CiOverview,
  CiReadinessPolicy,
  CiReadinessResult,
  CiReportFull,
  CiReportSummary,
  CiReviewReport,
  CiStatusReport,
  CiWorkflowRun,
  CvBaseline,
  CvCompare,
  CvEvent,
  CvGateView,
  CvOverview,
  CvPolicy,
  CvPreview,
  CvRunDetail,
  CvRunSummary,
  CvStartBody,
  PatchDetail,
  PatchItem,
  DuplicatePage,
  RefactorCatalog,
  RefactorDetail,
  RefactorDiff,
  RefactorDuplicatesRequest,
  RefactorItem,
  RefactorRenameRequest,
  RefactorRollback,
  RefactorVerification,
  RefactorVerifyPreview,
  RepairAuditEntry,
  RepairDetail,
  RepairItem,
  RepairRollback,
  RepairVerification,
  RepairVerifyPreview,
  ReviewChange,
  ReviewFinding,
  ReviewFindingStatus,
  ReviewImpact,
  ReviewItem,
  ReviewMode,
  ReviewPreview,
  ReviewRecommendations,
  ReviewRisk,
  ReviewSummaryOut,
  FailureDetail,
  FailureIngestResult,
  FailureItem,
  FailureStatus,
  InvestigationDetail,
  InvestigationItem,
  LocalizationOut,
  RelatedOut,
  VerificationItem,
  VerificationPreview,
  CoverageFileItem,
  CoverageOut,
  GapPage,
  PlanOut,
  TestDiscovery,
  TestExecutionRun,
  TestFileItem,
  TestGeneration,
  TestsOverview,
  AnalysisRun,
  AnalysisStatusOut,
  AnalysisSummary,
  Finding,
  FindingFilters,
  RulesOut,
  CyclesPage,
  DependenciesOut,
  DependencyOut,
  DiagnosticOut,
  FileDetail,
  Freshness,
  ImpactOut,
  IndexStatus,
  Metrics,
  SymbolDetail,
  ImportOut,
  IndexedFileOut,
  Overview,
  Page,
  Plan,
  PlanContent,
  RepositoryCreateInput,
  RepositoryDetail,
  RepositoryListItem,
  RepositorySummary,
  Snapshot,
  SymbolOut,
  SymbolSearchPage,
  Task,
  TaskCreateInput,
  TaskDetail,
  WorkspaceContext,
} from "../types/api";

const BASE = `${import.meta.env.VITE_API_BASE_URL ?? ""}/api/v1`;
const TOKEN_KEY = "nova.apiToken";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;
  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export function getStoredToken(): string {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setStoredToken(token: string): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: token simply is not remembered */
  }
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined) headers.set("Content-Type", "application/json");
  const token = getStoredToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, { ...init, headers });
  } catch (e) {
    // A cancelled (stale) request is not a network failure: re-throw so callers/react-query can discard it silently.
    if (init.signal?.aborted || (e instanceof DOMException && e.name === "AbortError")) throw e;
    throw new ApiError(0, "network_error", "Cannot reach the NOVA API. Is the backend running?");
  }
  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }
  if (!response.ok) {
    const err = (body as { error?: { code?: string; message?: string; details?: unknown } } | null)
      ?.error;
    throw new ApiError(
      response.status,
      err?.code ?? "http_error",
      err?.message ?? `Request failed with status ${response.status}.`,
      err?.details,
    );
  }
  return body as T;
}

export const qs = (params: Record<string, string | number | undefined>): string => {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "") sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
};

export interface ReviewBody { mode: ReviewMode; base?: string; head?: string; paths?: string[] }

const rp = (id: string, rid: string) => `/repositories/${id}/repairs/${rid}`;
const rf = (id: string, tid?: string) => `/repositories/${id}/refactors${tid ? `/${tid}` : ""}`;
const post = <T,>(path: string, body?: unknown) => request<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) });
const put = <T,>(path: string, body?: unknown) => request<T>(path, { method: "PUT", body: JSON.stringify(body ?? {}) });
const sec = (id: string, tail = "") => `/repositories/${id}/security${tail}`;
const ci = (id: string, tail = "") => `/repositories/${id}/ci${tail}`;
const vf = (id: string, tail = "") => `/repositories/${id}/verification${tail}`;
export interface RepairFilters { page?: number; q?: string; status?: string; severity?: string; verification_outcome?: string }

const ops = (tail = "") => `/ops${tail}`;

export const api = {
  opsOverview: () => request<OpsOverview>(ops("/overview")),
  opsHealth: (force = false) => request<OpsHealth>(ops(`/health${qs({ force: force ? "true" : undefined })}`)),
  opsSignals: () => request<{ signals: OpsSignal[] }>(ops("/signals")),
  opsIncidents: (f: { page?: number; status?: string } = {}) => request<Page<OpsIncident>>(ops(`/incidents${qs({ page: f.page, page_size: 10, status: f.status })}`)),
  opsIncident: (id: string) => request<OpsIncidentDetail>(ops(`/incidents/${id}`)),
  opsIncidentAction: (id: string, action: "acknowledge" | "investigate" | "resolve" | "reopen", note?: string) => post<OpsIncident>(ops(`/incidents/${id}/${action}`), { note }),
  opsEvaluate: () => post<{ evaluations: OpsAlertEval[]; continuous_monitoring: boolean }>(ops("/alerts/evaluate")),
  opsSlos: () => request<{ items: OpsSlo[] }>(ops("/slos")),
  opsLogs: (f: { level?: string; request_id?: string; q?: string } = {}) => request<{ items: OpsLogRecord[]; buffered: number; capacity: number; note: string }>(ops(`/logs${qs({ level: f.level, request_id: f.request_id, q: f.q, limit: 100 })}`)),
  opsTraces: (f: { request_id?: string } = {}) => request<{ items: OpsSpan[]; sample_ratio: number; buffered: number; exporter_enabled: boolean }>(ops(`/traces${qs({ request_id: f.request_id, limit: 100 })}`)),
  opsDiagnostics: () => request<{ findings: OpsDiagnostic[]; note: string; observed_since: string }>(ops("/diagnostics")),
  repairs: (id: string, f: RepairFilters = {}) => request<Page<RepairItem>>(`/repositories/${id}/repairs${qs({ page: f.page, page_size: 20, q: f.q, status: f.status, severity: f.severity, verification_outcome: f.verification_outcome })}`),
  createRepair: (id: string, body: { title: string; description?: string; severity: string; failure_id?: string; finding_id?: string; hint_files?: string[] }) => post<RepairItem>(`/repositories/${id}/repairs`, body),
  repair: (id: string, rid: string) => request<RepairDetail>(rp(id, rid)),
  setRepairStatus: (id: string, rid: string, status: "open" | "closed") => request<RepairItem>(rp(id, rid), { method: "PATCH", body: JSON.stringify({ status }) }),
  repairEvidence: (id: string, rid: string) => post<RepairDetail>(`${rp(id, rid)}/evidence`),
  repairProposal: (id: string, rid: string) => post<RepairDetail>(`${rp(id, rid)}/proposal`),
  repairAudit: (id: string, rid: string) => request<Page<RepairAuditEntry>>(`${rp(id, rid)}/audit${qs({ page_size: 50 })}`),
  repairPatches: (id: string, rid: string) => request<PatchItem[]>(`${rp(id, rid)}/patches`),
  repairPatch: (id: string, rid: string, pid: string) => request<PatchDetail>(`${rp(id, rid)}/patches/${pid}`),
  createPatch: (id: string, rid: string, body: { source: "generated" | "manual" | "ai_assisted"; diff?: string; title?: string; extra_files?: string[] }) => post<PatchDetail>(`${rp(id, rid)}/patches`, body),
  patchAction: (id: string, rid: string, pid: string, action: "validate" | "submit" | "approve") => post<PatchDetail>(`${rp(id, rid)}/patches/${pid}/${action}`),
  rejectPatch: (id: string, rid: string, pid: string, reason: string) => post<PatchDetail>(`${rp(id, rid)}/patches/${pid}/reject`, { reason }),
  applyPatch: (id: string, rid: string, pid: string) => post<PatchDetail>(`${rp(id, rid)}/patches/${pid}/apply`, { confirm: true }),
  patchRollback: (id: string, rid: string, pid: string) => request<RepairRollback>(`${rp(id, rid)}/patches/${pid}/rollback`),
  previewRepairVerification: (id: string, rid: string, pid: string, scope: "selected" | "all") => post<RepairVerifyPreview>(`${rp(id, rid)}/patches/${pid}/verify/preview`, { scope }),
  startRepairVerification: (id: string, rid: string, pid: string, scope: "selected" | "all") => post<RepairVerification>(`${rp(id, rid)}/patches/${pid}/verifications`, { confirm: true, scope }),
  repairVerifications: (id: string, rid: string, pid: string) => request<RepairVerification[]>(`${rp(id, rid)}/patches/${pid}/verifications`),
  refactorTypes: (id: string) => request<RefactorCatalog>(`${rf(id)}/types`),
  refactors: (id: string, f: { page?: number; status?: string; type_id?: string } = {}) => request<Page<RefactorItem>>(`${rf(id)}${qs({ page: f.page, page_size: 20, status: f.status, type_id: f.type_id })}`),
  refactor: (id: string, tid: string) => request<RefactorDetail>(rf(id, tid)),
  requestRename: (id: string, body: RefactorRenameRequest) => post<RefactorDetail>(`${rf(id)}/rename`, body),
  requestExtract: (id: string, body: { rel_path: string; start_line: number; end_line: number; new_name: string }) => post<RefactorDetail>(`${rf(id)}/extract-function`, body),
  requestDuplicates: (id: string, body: RefactorDuplicatesRequest) => post<RefactorDetail>(`${rf(id)}/duplicates`, body),
  requestSimplify: (id: string, rel_path: string) => post<RefactorDetail>(`${rf(id)}/simplify`, { rel_path }),
  requestCycles: (id: string, path_prefix: string) => post<RefactorDetail>(`${rf(id)}/cycle-suggestions`, { path_prefix }),
  refactorDiff: (id: string, tid: string) => request<RefactorDiff>(`${rf(id, tid)}/diff`),
  refactorDuplicates: (id: string, tid: string, f: { kind?: string; path?: string; min_similarity?: number; page?: number }) =>
    request<DuplicatePage>(`${rf(id, tid)}/duplicates${qs({ kind: f.kind, path: f.path, min_similarity: f.min_similarity || undefined, page: f.page, page_size: 10 })}`),
  validateRefactor: (id: string, tid: string) => post<RefactorDetail>(`${rf(id, tid)}/validate`),
  submitRefactor: (id: string, tid: string) => post<RefactorDetail>(`${rf(id, tid)}/submit`),
  approveRefactor: (id: string, tid: string, acknowledged_warnings: string[]) => post<RefactorDetail>(`${rf(id, tid)}/approve`, { acknowledged_warnings }),
  rejectRefactor: (id: string, tid: string, reason: string) => post<RefactorDetail>(`${rf(id, tid)}/reject`, { reason }),
  closeRefactor: (id: string, tid: string) => post<RefactorDetail>(`${rf(id, tid)}/close`),
  applyRefactor: (id: string, tid: string) => post<RefactorDetail>(`${rf(id, tid)}/apply`, { confirm: true }),
  refactorRollback: (id: string, tid: string) => request<RefactorRollback>(`${rf(id, tid)}/rollback`),
  refactorBaseline: (id: string, tid: string) => post<{ execution_run_id: string; status: string; note: string | null }>(`${rf(id, tid)}/baseline`, { confirm: true }),
  previewRefactorVerification: (id: string, tid: string, scope: "selected" | "all") => post<RefactorVerifyPreview>(`${rf(id, tid)}/verify/preview`, { scope }),
  secOverview: (id: string) => request<SecOverview>(sec(id, "/overview")),
  secStartScan: (id: string, body: { scan_types: string[]; exclude_globs: string[] }) => post<SecScan>(sec(id, "/scans"), body),
  secScans: (id: string) => request<Page<SecScan>>(sec(id, "/scans?page_size=10")),
  secScan: (id: string, sid: string) => request<SecScan>(sec(id, `/scans/${sid}`)),
  secComponents: (id: string, sid: string, f: { q?: string; assessment?: string; page?: number } = {}) => request<Page<SecComponent>>(sec(id, `/scans/${sid}/components${qs({ q: f.q, assessment: f.assessment, page: f.page, page_size: 25 })}`)),
  secFindings: (id: string, f: { kind?: string; severity?: string; status?: string; q?: string; sort?: string; page?: number } = {}) =>
    request<Page<SecFinding>>(sec(id, `/findings${qs({ kind: f.kind, severity: f.severity, status: f.status, q: f.q, sort: f.sort, page: f.page, page_size: 20 })}`)),
  secFinding: (id: string, fid: string) => request<SecFindingDetail>(sec(id, `/findings/${fid}`)),
  secTriage: (id: string, fid: string, body: { status: string; reason: string }) => post<SecFindingDetail>(sec(id, `/findings/${fid}/triage`), body),
  secAdvisoryMatches: (id: string) => request<Page<SecFindingDetail>>(sec(id, "/advisory-matches?page_size=50")),
  secSuppressions: (id: string) => request<SecSuppression[]>(sec(id, "/suppressions")),
  secSuppress: (id: string, body: Record<string, unknown>) => post<SecSuppression>(sec(id, "/suppressions"), body),
  secRevokeSuppression: (id: string, sid: string, reason: string) => post<SecSuppression>(sec(id, `/suppressions/${sid}/revoke`), { reason }),
  secExceptions: (id: string) => request<SecException[]>(sec(id, "/exceptions")),
  secAddException: (id: string, body: Record<string, unknown>) => post<SecException>(sec(id, "/exceptions"), body),
  secRevokeException: (id: string, xid: string) => post<SecException>(sec(id, `/exceptions/${xid}/revoke`)),
  secPolicy: (id: string) => request<SecPolicyView>(sec(id, "/policy")),
  secPutPolicy: (id: string, rules: Record<string, unknown>, note: string) => put<SecPolicyView>(sec(id, "/policy"), { rules, note }),
  secEvaluate: (id: string) => post<SecEvaluation>(sec(id, "/evaluate"), {}),
  secPropose: (id: string, fid: string) => post<SecRemediation>(sec(id, `/findings/${fid}/remediations`)),
  secRemediations: (id: string, fid: string) => request<SecRemediation[]>(sec(id, `/remediations${qs({ finding_id: fid })}`)),
  secRemediation: (id: string, rid: string) => request<SecRemediation>(sec(id, `/remediations/${rid}`)),
  secSubmitRemediation: (id: string, rid: string) => post<SecRemediation>(sec(id, `/remediations/${rid}/submit`)),
  secMakeReport: (id: string, format: string) => post<SecReportSummary>(sec(id, "/reports"), { format }),
  secReport: (id: string, rid: string) => request<SecReportSummary>(sec(id, `/reports/${rid}`)),
  ciOverview: (id: string) => request<CiOverview>(ci(id, "/overview")),
  ciSaveConnection: (id: string, body: Record<string, unknown>) => post<CiConnection>(ci(id, "/connections"), body),
  ciValidate: (id: string, cid: string) => post<CiConnection>(ci(id, `/connections/${cid}/validate`)),
  ciSync: (id: string, cid: string) => post<{ created: number; updated: number; head_changed: number; closed: number; branches: number }>(ci(id, `/connections/${cid}/sync`)),
  ciChangeRequests: (id: string, f: { state?: string; q?: string; page?: number } = {}) => request<Page<CiChangeRequest>>(ci(id, `/change-requests${qs({ state: f.state, q: f.q, page: f.page, page_size: 20 })}`)),
  ciChangeRequest: (id: string, crid: string) => request<CiChangeRequest>(ci(id, `/change-requests/${crid}`)),
  ciStartWorkflow: (id: string, crid: string, body: { confirm: boolean; mode?: string; force?: boolean }) => post<CiWorkflowRun>(ci(id, `/change-requests/${crid}/workflow-runs`), body),
  ciWorkflows: (id: string, crid: string) => request<CiWorkflowRun[]>(ci(id, `/change-requests/${crid}/workflow-runs`)),
  ciWorkflow: (id: string, wid: string) => request<CiWorkflowRun>(ci(id, `/workflow-runs/${wid}`)),
  ciReadiness: (id: string, wid: string) => request<CiReadinessResult>(ci(id, `/workflow-runs/${wid}/readiness`)),
  ciReviewReport: (id: string, crid: string) => request<CiReviewReport>(ci(id, `/change-requests/${crid}/review-report`)),
  ciMakeReport: (id: string, crid: string, kind: "review" | "release") => post<CiReportSummary>(ci(id, `/change-requests/${crid}/reports`), { kind }),
  ciReports: (id: string, crid?: string) => request<CiReportSummary[]>(ci(id, `/reports${qs({ change_request_id: crid })}`)),
  ciReport: (id: string, rid: string) => request<CiReportFull>(ci(id, `/reports/${rid}`)),
  ciStatusReports: (id: string, wid: string) => request<CiStatusReport[]>(ci(id, `/workflow-runs/${wid}/status-reports`)),
  ciSendStatus: (id: string, wid: string) => post<CiStatusReport>(ci(id, `/workflow-runs/${wid}/status-reports`), { confirm: true }),
  ciRetryStatus: (id: string, rid: string) => post<CiStatusReport>(ci(id, `/status-reports/${rid}/retry`)),
  ciReadinessPolicy: (id: string) => request<CiReadinessPolicy>(ci(id, "/readiness-policy")),
  cvOverview: (id: string) => request<CvOverview>(vf(id, "/overview")),
  cvPolicy: (id: string) => request<CvPolicy>(vf(id, "/policy")),
  cvPutPolicy: (id: string, rules: Record<string, unknown>, note: string) => put<CvPolicy>(vf(id, "/policy"), { rules, note }),
  cvPreview: (id: string, body: Omit<CvStartBody, "confirm">) => post<CvPreview>(vf(id, "/changes/preview"), body),
  cvStart: (id: string, body: CvStartBody) => post<CvRunDetail>(vf(id, "/runs"), body),
  cvRuns: (id: string, f: { page?: number; outcome?: string; status?: string; kind?: string } = {}) => request<Page<CvRunSummary>>(vf(id, `/runs${qs({ page: f.page, page_size: 20, outcome: f.outcome, status: f.status, kind: f.kind })}`)),
  cvRun: (id: string, rid: string) => request<CvRunDetail>(vf(id, `/runs/${rid}`)),
  cvEvents: (id: string, rid: string) => request<CvEvent[]>(vf(id, `/runs/${rid}/events`)),
  cvCancel: (id: string, rid: string) => post<CvRunDetail>(vf(id, `/runs/${rid}/cancel`)),
  cvCompare: (id: string, rid: string, baselineId: string) => request<CvCompare>(vf(id, `/runs/${rid}/compare${qs({ baseline_id: baselineId })}`)),
  cvGate: (id: string, rid: string) => request<CvGateView>(vf(id, `/runs/${rid}/gate`)),
  cvBaselines: (id: string) => request<CvBaseline[]>(vf(id, "/baselines")),
  cvCreateBaseline: (id: string, label: string, coverage: boolean) => post<CvRunDetail>(vf(id, "/baselines"), { label, coverage, confirm: true }),
  startRefactorVerification: (id: string, tid: string, scope: "selected" | "all") => post<RefactorVerification>(`${rf(id, tid)}/verifications`, { confirm: true, scope }),
  refactorVerifications: (id: string, tid: string) => request<RefactorVerification[]>(`${rf(id, tid)}/verifications`),
  refactorAudit: (id: string, tid: string) => request<Page<RepairAuditEntry>>(`${rf(id, tid)}/audit${qs({ page_size: 50 })}`),
  reviewPreview: (id: string, body: ReviewBody) => request<ReviewPreview>(`/repositories/${id}/reviews/preview`, { method: "POST", body: JSON.stringify(body) }),
  createReview: (id: string, body: ReviewBody) => request<ReviewItem>(`/repositories/${id}/reviews`, { method: "POST", body: JSON.stringify(body) }),
  reviews: (id: string, page = 1) => request<Page<ReviewItem>>(`/repositories/${id}/reviews${qs({ page, page_size: 20 })}`),
  review: (id: string, rid: string) => request<ReviewItem & { stages: { name: string; status: string; detail?: string }[] }>(`/repositories/${id}/reviews/${rid}`),
  reviewSummary: (id: string, rid: string) => request<ReviewSummaryOut>(`/repositories/${id}/reviews/${rid}/summary`),
  reviewChanges: (id: string, rid: string, page = 1) => request<Page<ReviewChange>>(`/repositories/${id}/reviews/${rid}/changes${qs({ page, page_size: 50 })}`),
  reviewFindings: (id: string, rid: string, o: { page?: number; severity?: string; category?: string; status?: string; confidence?: string } = {}) =>
    request<Page<ReviewFinding>>(`/repositories/${id}/reviews/${rid}/findings${qs({ page: o.page, page_size: 20, severity: o.severity, category: o.category, status: o.status, confidence: o.confidence })}`),
  setReviewFinding: (id: string, rid: string, fid: string, body: { status: ReviewFindingStatus; rationale?: string }) =>
    request<ReviewFinding>(`/repositories/${id}/reviews/${rid}/findings/${fid}`, { method: "PATCH", body: JSON.stringify(body) }),
  reviewRisk: (id: string, rid: string) => request<ReviewRisk>(`/repositories/${id}/reviews/${rid}/risk`),
  reviewImpact: (id: string, rid: string) => request<ReviewImpact>(`/repositories/${id}/reviews/${rid}/impact`),
  reviewRecommendations: (id: string, rid: string) => request<ReviewRecommendations>(`/repositories/${id}/reviews/${rid}/test-recommendations${qs({ page_size: 50 })}`),
  workspace: () => request<WorkspaceContext>("/workspace"),
  overview: () => request<Overview>("/overview"),
  ready: () => request<{ status: string; checks: Record<string, string | null> }>("/ready"),

  listRepositories: (page = 1, pageSize = 50) =>
    request<Page<RepositoryListItem>>(`/repositories${qs({ page, page_size: pageSize })}`),
  getRepository: (id: string) => request<RepositoryDetail>(`/repositories/${id}`),
  registerRepository: (input: RepositoryCreateInput) =>
    request<RepositoryDetail>("/repositories", { method: "POST", body: JSON.stringify(input) }),
  startIndexing: (id: string, mode: "incremental" | "full" = "incremental") =>
    request<Snapshot>(`/repositories/${id}/index`, { method: "POST", body: JSON.stringify({ mode }) }),
  getSnapshot: (repoId: string, snapshotId: string) =>
    request<Snapshot>(`/repositories/${repoId}/snapshots/${snapshotId}`),
  repositorySummary: (id: string) => request<RepositorySummary>(`/repositories/${id}/summary`),
  listFiles: (
    id: string,
    opts: { page: number; pageSize: number; q?: string; language?: string; analysisStatus?: string },
    signal?: AbortSignal,
  ) =>
    request<Page<IndexedFileOut>>(
      `/repositories/${id}/files${qs({ page: opts.page, page_size: opts.pageSize, q: opts.q, language: opts.language, analysis_status: opts.analysisStatus })}`,
      { signal },
    ),
  freshness: (id: string) => request<Freshness>(`/repositories/${id}/freshness`),
  searchSymbols: (id: string, opts: { q: string; kind?: string; page?: number }) =>
    request<SymbolSearchPage>(
      `/repositories/${id}/symbols/search${qs({ q: opts.q, kind: opts.kind, page: opts.page ?? 1, page_size: 20 })}`,
    ),
  fileDetail: (id: string, fileId: string) => request<FileDetail>(`/repositories/${id}/files/${fileId}`),
  fileSymbols: (id: string, fileId: string) =>
    request<Page<SymbolOut>>(`/repositories/${id}/symbols${qs({ file_id: fileId, page_size: 200 })}`),
  fileImports: (id: string, fileId: string) =>
    request<Page<ImportOut>>(`/repositories/${id}/files/${fileId}/imports?page_size=200`),
  fileDependencies: (id: string, fileId: string) =>
    request<Page<DependencyOut>>(`/repositories/${id}/files/${fileId}/dependencies?page_size=200`),
  fileDependents: (id: string, fileId: string) =>
    request<Page<DependencyOut>>(`/repositories/${id}/files/${fileId}/dependents?page_size=200`),
  listSymbols: (id: string, opts: { q?: string; kind?: string; page?: number; pageSize?: number }, signal?: AbortSignal) =>
    request<Page<SymbolOut>>(
      `/repositories/${id}/symbols${qs({ q: opts.q, kind: opts.kind, page: opts.page ?? 1, page_size: opts.pageSize ?? 20 })}`,
      { signal },
    ),
  symbolDetail: (id: string, symbolId: string) =>
    request<SymbolDetail>(`/repositories/${id}/symbols/${encodeURIComponent(symbolId)}`),
  dependencies: (
    id: string,
    opts: { path: string; direction: "out" | "in"; depth: number; page?: number },
  ) =>
    request<DependenciesOut>(
      `/repositories/${id}/dependencies${qs({ path: opts.path, direction: opts.direction, depth: opts.depth, page: opts.page ?? 1, page_size: 25 })}`,
    ),
  impact: (
    id: string,
    opts: { path?: string; symbolId?: string; maxDepth: number; page?: number },
  ) =>
    request<ImpactOut>(
      `/repositories/${id}/dependencies/impact${qs({ path: opts.path, symbol_id: opts.symbolId, max_depth: opts.maxDepth, page: opts.page ?? 1, page_size: 25 })}`,
    ),
  cycles: (id: string, page = 1) =>
    request<CyclesPage>(`/repositories/${id}/dependencies/cycles${qs({ page, page_size: 10 })}`),
  indexStatus: (id: string, includePending = false) =>
    request<IndexStatus>(`/repositories/${id}/index/status${qs({ include_pending: includePending ? "true" : undefined })}`),
  rebuildIndex: (id: string) => request<Snapshot>(`/repositories/${id}/index/rebuild`, { method: "POST" }),
  metrics: (id: string, includePending = false) =>
    request<Metrics>(`/repositories/${id}/metrics${qs({ include_pending: includePending ? "true" : undefined })}`),
  snapshotDiagnostics: (id: string, snapshotId: string) =>
    request<Page<DiagnosticOut>>(`/repositories/${id}/snapshots/${snapshotId}/diagnostics?page_size=100`),

  startAnalysis: (id: string, mode: "incremental" | "full") =>
    request<AnalysisRun>(`/repositories/${id}/analysis`, { method: "POST", body: JSON.stringify({ mode }) }),
  analysisStatus: (id: string) => request<AnalysisStatusOut>(`/repositories/${id}/analysis/status`),
  analysisSummary: (id: string) => request<AnalysisSummary>(`/repositories/${id}/analysis/summary`),
  analysisRules: (id: string) => request<RulesOut>(`/repositories/${id}/analysis/rules`),
  findings: (id: string, f: FindingFilters) =>
    request<Page<Finding>>(
      `/repositories/${id}/findings${qs({ severity: f.severity, category: f.category, status: f.status, rule_id: f.ruleId, path: f.path, q: f.q, sort: f.sort ?? "severity", order: f.order ?? "asc", page: f.page ?? 1, page_size: 20 })}`,
    ),
  updateFinding: (id: string, findingId: string, body: { status: "suppressed" | "existing"; reason?: string }) =>
    request<Finding>(`/repositories/${id}/findings/${findingId}`, { method: "PATCH", body: JSON.stringify(body) }),

  failures: (id: string, o: { page?: number; status?: string; category?: string; q?: string; sort?: string; order?: string } = {}) =>
    request<Page<FailureItem>>(`/repositories/${id}/failures${qs({ page: o.page, page_size: 20, status: o.status, category: o.category, q: o.q, sort: o.sort, order: o.order })}`),
  failure: (id: string, fid: string) => request<FailureDetail>(`/repositories/${id}/failures/${fid}`),
  ingestFailure: (id: string, body: { source: string; text?: string; execution_run_id?: string; test_id?: string }) =>
    request<FailureIngestResult>(`/repositories/${id}/failures/ingest`, { method: "POST", body: JSON.stringify(body) }),
  setFailureStatus: (id: string, fid: string, status: FailureStatus) =>
    request<FailureItem>(`/repositories/${id}/failures/${fid}`, { method: "PATCH", body: JSON.stringify({ status }) }),
  localization: (id: string, fid: string) => request<LocalizationOut>(`/repositories/${id}/failures/${fid}/localization`),
  relatedFailures: (id: string, fid: string) => request<RelatedOut>(`/repositories/${id}/failures/${fid}/related`),
  createInvestigation: (id: string, fid: string) =>
    request<InvestigationDetail>(`/repositories/${id}/failures/${fid}/investigations`, { method: "POST" }),
  investigations: (id: string, status?: string) => request<Page<InvestigationItem>>(`/repositories/${id}/investigations${qs({ status, page_size: 100 })}`),
  investigation: (id: string, iid: string) => request<InvestigationDetail>(`/repositories/${id}/investigations/${iid}`),
  patchInvestigation: (id: string, iid: string, body: { status?: string; notes?: string; resolution?: string }) =>
    request<InvestigationItem>(`/repositories/${id}/investigations/${iid}`, { method: "PATCH", body: JSON.stringify(body) }),
  previewVerification: (id: string, iid: string, testIds?: string[]) =>
    request<VerificationPreview>(`/repositories/${id}/investigations/${iid}/verification`, { method: "POST", body: JSON.stringify({ confirm: false, test_ids: testIds }) }),
  startVerification: (id: string, iid: string, testIds?: string[]) =>
    request<VerificationItem>(`/repositories/${id}/investigations/${iid}/verification`, { method: "POST", body: JSON.stringify({ confirm: true, test_ids: testIds }) }),
  verifications: (id: string, iid: string) =>
    request<{ items: VerificationItem[]; outcomes: string[] }>(`/repositories/${id}/investigations/${iid}/verifications`),

  testsOverview: (id: string) => request<TestsOverview>(`/repositories/${id}/tests`),
  discoverTests: (id: string) => request<TestDiscovery>(`/repositories/${id}/tests/discover`, { method: "POST" }),
  testFiles: (id: string, page = 1) => request<Page<TestFileItem>>(`/repositories/${id}/tests/files${qs({ page, page_size: 20 })}`),
  planTests: (id: string, path: string, symbol?: string) =>
    request<PlanOut>(`/repositories/${id}/tests/plan`, { method: "POST", body: JSON.stringify({ targets: [{ path, symbol: symbol || null }] }) }),
  generateTests: (id: string, path: string, symbol?: string) =>
    request<TestGeneration>(`/repositories/${id}/tests/generate`, { method: "POST", body: JSON.stringify({ targets: [{ path, symbol: symbol || null }] }) }),
  generations: (id: string) => request<Page<TestGeneration>>(`/repositories/${id}/tests/generations${qs({ page_size: 20 })}`),
  generation: (id: string, genId: string) => request<TestGeneration>(`/repositories/${id}/tests/generations/${genId}`),
  validateGeneration: (id: string, genId: string) =>
    request<TestGeneration>(`/repositories/${id}/tests/generations/${genId}/validate`, { method: "POST" }),
  approveGeneration: (id: string, genId: string, candidateIds?: string[]) =>
    request<TestGeneration>(`/repositories/${id}/tests/generations/${genId}/approve`, { method: "POST", body: JSON.stringify({ candidate_ids: candidateIds ?? null }) }),
  executeGeneration: (id: string, genId: string, coverage: boolean) =>
    request<TestExecutionRun>(`/repositories/${id}/tests/generations/${genId}/execute`, { method: "POST", body: JSON.stringify({ coverage }) }),
  executeExisting: (id: string, coverage: boolean) =>
    request<TestExecutionRun>(`/repositories/${id}/tests/execute`, { method: "POST", body: JSON.stringify({ confirm: true, coverage }) }),
  testRuns: (id: string) => request<Page<TestExecutionRun>>(`/repositories/${id}/tests/runs${qs({ page_size: 20 })}`),
  coverage: (id: string) => request<CoverageOut>(`/repositories/${id}/coverage`),
  coverageFiles: (id: string, page = 1) =>
    request<Page<CoverageFileItem>>(`/repositories/${id}/coverage/files${qs({ page, page_size: 15 })}`),
  coverageGaps: (id: string, page = 1) => request<GapPage>(`/repositories/${id}/coverage/gaps${qs({ page, page_size: 10 })}`),

  listTasks: (page = 1, pageSize = 20) =>
    request<Page<Task>>(`/tasks${qs({ page, page_size: pageSize })}`),
  getTask: (id: string) => request<TaskDetail>(`/tasks/${id}`),
  createTask: (input: TaskCreateInput) =>
    request<TaskDetail>("/tasks", { method: "POST", body: JSON.stringify(input) }),
  savePlan: (id: string, content: PlanContent) =>
    request<TaskDetail & { plan: Plan }>(`/tasks/${id}/plan`, {
      method: "PUT",
      body: JSON.stringify({ content }),
    }),
  regeneratePlan: (id: string) =>
    request<TaskDetail>(`/tasks/${id}/plan/regenerate`, { method: "POST" }),
};

/** Turn a thrown value into a short message, including field-level validation messages. */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (Array.isArray(error.details) && error.details.length > 0) {
      const lines = (error.details as { field?: string; message?: string }[])
        .map((d) => (d.field ? `${d.field}: ${d.message}` : d.message))
        .filter(Boolean);
      if (lines.length) return `${error.message} ${lines.join("; ")}`;
    }
    return error.message;
  }
  return error instanceof Error ? error.message : "Something went wrong.";
}
