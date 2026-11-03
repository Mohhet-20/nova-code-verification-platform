// Mirrors backend/app/schemas. Keep in sync when the API changes.
export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
}

export interface WorkspaceContext {
  workspace_id: string;
  workspace_name: string;
  user_handle: string;
  user_display_name: string;
  role: string;
  auth_mode: "local-trust" | "bearer-token";
  multi_user_isolation: boolean;
  ai_provider: string;
  planner: string;
  version: string;
  environment: string;
  limits: Record<string, number>;
  registration_restricted: boolean;
}

export interface AuditEvent {
  id: string;
  action: string;
  entity_type: string;
  entity_id: string;
  details: Record<string, unknown>;
  created_at: string;
}

export interface Overview {
  repositories: number;
  indexed_repositories: number;
  indexed_files: number;
  tasks_total: number;
  tasks_by_status: Record<string, number>;
  recent_events: AuditEvent[];
}

export interface Snapshot {
  id: string;
  repository_id: string;
  status: "running" | "completed" | "completed_with_warnings" | "failed";
  started_at: string;
  finished_at: string | null;
  files_indexed: number;
  directories_visited: number;
  total_bytes: number;
  total_lines: number;
  skipped_counts: Record<string, number>;
  language_counts: Record<string, number>;
  truncated: boolean;
  truncation_reason: string | null;
  error_message: string | null;
  limits: Record<string, number>;
  git_head: string | null;
  git_branch: string | null;
  generation: number;
  mode: "incremental" | "full";
  files_new: number;
  files_changed: number;
  files_unchanged: number;
  files_removed: number | null;
  files_failed: number;
  files_skipped: number;
  symbols_count: number;
  imports_total: number;
  imports_unresolved: number;
  relationships_count: number;
  analysis_counts: Record<string, number>;
  warnings: string[];
  duration_ms: number | null;
}

export type AnalysisStatus = "parsed" | "partial" | "failed" | "unavailable" | "unsupported";

export interface RepositoryListItem {
  id: string;
  name: string;
  description: string;
  root_label: string;
  is_git: boolean;
  git_branch: string | null;
  created_at: string;
  latest_snapshot_status: Snapshot["status"] | null;
  latest_snapshot_at: string | null;
  files_indexed: number | null;
}

export interface RepositoryDetail {
  id: string;
  name: string;
  description: string;
  root_path: string;
  root_label: string;
  is_git: boolean;
  git_branch: string | null;
  git_head: string | null;
  git_dirty_count: number | null;
  created_at: string;
  updated_at: string;
  latest_snapshot: Snapshot | null;
}

export interface RepositorySummary {
  repository_id: string;
  indexed: boolean;
  snapshot: Snapshot | null;
  top_languages: [string, number][];
  largest_files: [string, number][];
  symbol_kinds: [string, number][];
  index_state: "none" | "ready" | "partial";
}

export interface IndexedFileOut {
  id: string;
  change_status: "new" | "changed" | "unchanged";
  analysis_status: AnalysisStatus;
  language_confidence: string;
  parser: string | null;
  symbol_count: number;
  import_count: number;
  rel_path: string;
  extension: string;
  language: string;
  size_bytes: number;
  line_count: number;
  depth: number;
}

export type StepKind = "investigate" | "implement" | "test" | "verify" | "review";

export interface PlanStep {
  id: string;
  kind: StepKind;
  title: string;
  description: string;
  target_paths: string[];
  requires_approval: boolean;
}

export interface ExpectedCheck {
  name: string;
  suggested_command: string | null;
  rationale: string;
  executed: boolean;
}

export interface PlanContent {
  summary: string;
  confidence: "low" | "medium" | "high";
  relevant_files: string[];
  steps: PlanStep[];
  expected_checks: ExpectedCheck[];
  assumptions: string[];
  warnings: string[];
}

export interface Plan {
  generator: string;
  generator_version: string;
  revision: number;
  edited_by_user: boolean;
  snapshot_id: string | null;
  updated_at: string;
  content: PlanContent;
}

export interface Task {
  id: string;
  repository_id: string;
  repository_name: string;
  title: string;
  problem_statement: string;
  objective: string;
  scope: string[];
  acceptance_criteria: string[];
  status: string;
  created_at: string;
  updated_at: string;
}

export interface TaskDetail extends Task {
  plan: Plan | null;
}

export interface RepositoryCreateInput {
  name: string;
  root_path: string;
  description: string;
}

export interface TaskCreateInput {
  repository_id: string;
  title: string;
  problem_statement: string;
  objective: string;
  scope: string[];
  acceptance_criteria: string[];
}

export interface IndexRef {
  snapshot_id: string;
  generation: number;
  status: string;
  finished_at: string | null;
}

export interface SymbolOut {
  id: string;
  stable_id: string;
  name: string;
  qualified_name: string;
  kind: string;
  language: string;
  file_id: string;
  file_path: string;
  start_line: number;
  start_col: number;
  end_line: number;
  end_col: number;
  parent_stable_id: string | null;
  visibility: string | null;
  exported: boolean;
  signature: string | null;
  doc_summary: string | null;
  bases: string[];
  parser: string;
  extraction_method: string;
  confidence: string;
}

export interface SymbolHit extends SymbolOut {
  matched_on: string;
}

export interface SymbolSearchPage extends Page<SymbolHit> {
  index: IndexRef | null;
  ambiguous: boolean;
}

export interface ImportOut {
  id: string;
  kind: string;
  specifier: string;
  names: { name?: string | null; alias?: string | null }[];
  level: number;
  line: number;
  type_only: boolean;
  resolution_status: "resolved" | "external" | "unresolved" | "unsupported";
  resolution_note: string | null;
  package: string | null;
  resolved_path: string | null;
}

export interface DependencyOut {
  type: string;
  status: string;
  evidence: string;
  path: string | null;
  target_text: string;
  symbol_stable_id: string | null;
}

export interface FileDetail {
  id: string;
  rel_path: string;
  extension: string;
  language: string;
  language_confidence: string;
  size_bytes: number;
  line_count: number;
  sha256: string;
  change_status: string;
  analysis_status: AnalysisStatus;
  parser: string | null;
  analysis_version: number;
  parse_diagnostics: string[];
  symbol_count: number;
  import_count: number;
  dependency_count: number;
  dependent_count: number;
  index: IndexRef;
}

export interface DiagnosticOut {
  rel_path: string | null;
  code: string;
  severity: "info" | "warning" | "error";
  detail: string | null;
}

export interface Freshness {
  state: "never_indexed" | "running" | "fresh" | "stale" | "unknown";
  snapshot_id: string | null;
  generation: number | null;
  indexed_at: string | null;
  last_attempt_status: string | null;
  changed: number | null;
  new: number | null;
  removed: number | null;
  sample_paths: string[];
  git_head_changed: boolean | null;
  note: string;
}

// ---- Phase 03: dependency graph, impact, cycles, status and metrics ----

export interface GraphItem {
  kind: "file" | "symbol" | "module" | string;
  id: string;
  name: string;
  path: string;
  depth: number;
  relationship: "IMPORTS_FILE" | "CALLS" | "EXTENDS_SYMBOL" | string;
  status: string;
  evidence: string;
  line: number | null;
  chain: string[];
  explanation: string;
}

export interface UnresolvedImport {
  specifier: string;
  kind: string;
  status: string;
  note: string | null;
  line: number;
  package: string | null;
}

export interface DependenciesOut extends Page<GraphItem> {
  root: { file_id: string; path: string };
  direction: "out" | "in";
  max_depth: number;
  truncated: boolean;
  more_beyond_depth: boolean;
  unresolved: UnresolvedImport[];
  unresolved_total: number;
  index: IndexRef;
  note: string;
}

export interface CycleRef {
  id: string;
  size: number;
  severity: "info" | "medium" | "high";
  files: string[];
}

export interface ImpactOut extends Page<GraphItem> {
  target_kind: "file" | "symbol";
  target_id: string;
  target_name: string;
  target_path: string;
  max_depth: number;
  summary: { direct: number; transitive: number; total: number; files_affected: number; max_depth_reached: number };
  truncated: boolean;
  more_beyond_depth: boolean;
  cycles: CycleRef[];
  disclaimer: string;
  index: IndexRef;
}

export interface CycleOut {
  id: string;
  kind: string;
  size: number;
  severity: "info" | "medium" | "high";
  severity_reason: string;
  files: { file_id: string; path: string }[];
  files_truncated: boolean;
  edges: { from: string; to: string }[];
  explanation: string;
}

export interface CyclesPage extends Page<CycleOut> {
  severity_rules: Record<string, string>;
  note: string;
  index: IndexRef | null;
}

export interface SymbolLink {
  stable_id: string | null;
  name: string;
  qualified_name: string | null;
  kind: string | null;
  path: string | null;
  line: number | null;
  relationship: string;
  status: string;
  evidence: string;
}

export interface SymbolDetail extends SymbolOut {
  children: SymbolOut[];
  callers: SymbolLink[];
  callees: SymbolLink[];
  base_links: SymbolLink[];
  subclasses: SymbolLink[];
  caller_total: number;
  callee_total: number;
  unresolved_call_total: number;
  index: IndexRef;
  note: string;
}

export interface IndexStatus {
  state: "never_indexed" | "running" | "completed" | "completed_with_warnings" | "failed";
  serving: IndexRef | null;
  latest: Snapshot | null;
  last_failure: string | null;
  message: string;
  pending: Freshness | null;
  files_awaiting_indexing: number | null;
  analysis_version: number;
}

export interface Metrics {
  indexed: boolean;
  index: IndexRef | null;
  generated_at: string;
  files: number;
  files_by_language: Record<string, number>;
  files_by_analysis_status: Record<string, number>;
  parse_failures: number;
  symbols: number;
  symbols_by_kind: Record<string, number>;
  edges_by_type: Record<string, number>;
  imports_by_status: Record<string, number>;
  calls_by_status: Record<string, number>;
  cycles: number;
  max_dependency_depth: number;
  files_awaiting_indexing: number | null;
  definitions: Record<string, string>;
}

// ---- Phase 04: static analysis, findings and code health ----

export type Severity = "critical" | "high" | "medium" | "low" | "info";
export type FindingStatus = "new" | "existing" | "resolved" | "reopened" | "suppressed";

export interface AnalysisRun {
  id: string;
  repository_id: string;
  index_snapshot_id: string | null;
  status: "running" | "completed" | "completed_with_warnings" | "failed";
  mode: string;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  files_eligible: number;
  files_analyzed: number;
  files_unchanged: number;
  files_excluded: number;
  files_skipped: number;
  files_parse_failed: number;
  files_failed: number;
  files_not_reached: number;
  findings_new: number;
  findings_reopened: number;
  findings_resolved: number;
  findings_open: number;
  rules_enabled: number;
  rules_disabled: number;
  config_source: string;
  analyzer_versions: Record<string, string>;
  warnings: string[];
  analyzer_errors: { analyzer_id: string; rule_id: string; error_type: string; files: number; first_file: string }[];
  error_message: string | null;
}

export interface ConfigState {
  valid: boolean;
  source: string;
  errors: string[];
}

export interface AnalysisStatusOut {
  state: "never_analyzed" | "running" | "completed" | "completed_with_warnings" | "failed";
  latest: AnalysisRun | null;
  findings_open: number;
  index_generation: number | null;
  analyzed_index_generation: number | null;
  index_changed_since_run: boolean | null;
  config: ConfigState;
  message: string;
}

export interface Finding {
  id: string;
  repository_id: string;
  rule_id: string;
  analyzer_id: string;
  rel_path: string;
  file_id: string | null;
  language: string;
  start_line: number;
  end_line: number;
  start_col: number | null;
  end_col: number | null;
  symbol_stable_id: string | null;
  scope: string;
  severity: Severity;
  category: string;
  title: string;
  explanation: string;
  evidence: string;
  remediation: string;
  limitations: string;
  confidence: "high" | "medium" | "low";
  metric_value: number | null;
  fingerprint: string;
  status: FindingStatus;
  resolution_reason: string | null;
  suppression_reason: string | null;
  reopened_count: number;
  first_seen_at: string;
  last_seen_at: string;
  resolved_at: string | null;
  analyzer_version: string;
  rule_version: string;
  first_run_id: string;
  last_run_id: string;
}

export interface AnalysisSummary {
  analyzed: boolean;
  latest_run: AnalysisRun | null;
  total_findings: number;
  open: number;
  by_severity: Record<string, number>;
  by_category: Record<string, number>;
  by_status: Record<string, number>;
  resolved_by_reason: Record<string, number>;
  top_rules: { key: string; count: number }[];
  files_analyzed: number;
  analyzer_errors: number;
  warnings: number;
  notes: string[];
}

export interface FindingFilters {
  severity?: string;
  category?: string;
  status?: string;
  ruleId?: string;
  path?: string;
  q?: string;
  sort?: string;
  order?: "asc" | "desc";
  page?: number;
}

export interface RuleInfo {
  id: string;
  title: string;
  category: string;
  severity: string;
  effective_severity: string;
  enabled: boolean;
  support: string;
}
export interface RulesOut {
  rules: RuleInfo[];
  config: ConfigState;
  languages: { language: string; status: string; note: string }[];
}

// ---- Test engineering (Phase 05) ----
export interface TestDiscovery {
  id: string; status: string; framework: string; framework_config: Record<string, unknown>;
  started_at: string; finished_at: string | null; files_found: number; files_unparsable: number; tests_found: number;
  source_files_with_tests: number; source_files_total: number; errors: string[]; excluded: string[];
}
export interface TestFileItem {
  id: string; rel_path: string; framework: string; parse_status: string; parse_error: string | null; test_count: number;
  tests: { id: string; name: string; line: number; parametrized: boolean }[];
  associated_sources: { path: string; via: string; confidence: string }[]; notes: string[];
}
export interface TestsOverview {
  discovery: TestDiscovery | null; note: string; generation_runs: number;
  candidates_by_status: Record<string, number>; execution_runs_by_status: Record<string, number>;
  latest_coverage_run_id: string | null;
}
export interface PlannedCase {
  id: string; test_name: string; target_symbol: string; category: string; title: string; purpose: string;
  preconditions: string; input_strategy: string; expected: string; rationale: string; confidence: string;
  assumptions: string[]; review_required: boolean; runnable: boolean;
}
export interface PlanOut { targets: Record<string, unknown>[]; note: string }
export interface TestCandidate {
  id: string; target_path: string; target_symbol: string | null; module: string | null; cases: PlannedCase[];
  confidence: string; review_required: boolean; runnable_count: number; size_bytes: number; content_sha256: string;
  status: string; validation: { check: string; passed: boolean; detail: string }[]; rejection_reasons: string[];
  warnings: string[]; approved_at: string | null; exported_path: string | null; source: string | null;
}
export interface TestGeneration {
  id: string; status: string; targets: { path: string; symbol: string | null }[]; generator_version: string;
  tool_versions: Record<string, unknown>; warnings: string[]; created_at: string; candidates: TestCandidate[]; created: boolean;
}
export interface TestExecutionRun {
  id: string; kind: "generated" | "existing"; generation_run_id: string | null; status: string; reason: string | null;
  created_at: string; started_at: string | null; finished_at: string | null; duration_ms: number | null; exit_code: number | null;
  command: string[]; environment_profile: Record<string, unknown>;
  tests_collected: number | null; tests_passed: number | null; tests_failed: number | null; tests_errors: number | null;
  tests_skipped: number | null; failures: { name: string; message?: string; traceback?: string }[];
  stdout_tail: string; stderr_tail: string; output_truncated: boolean;
  coverage_status: string; coverage_error: string | null; coverage_tool: string | null; coverage_branch: boolean;
  lines_total: number | null; lines_covered: number | null; line_percent: number | null;
  branches_total: number | null; branches_covered: number | null; branch_percent: number | null;
  tool_versions: Record<string, unknown>;
}
export interface CoverageOut {
  state: "never_measured" | "measured"; message: string; run: TestExecutionRun | null; unmeasured_targets: string[]; basis: string | null;
}
export interface CoverageFileItem {
  rel_path: string; statements: number; covered: number; line_percent: number; missing_lines: number[][];
  branches: number | null; branches_covered: number | null;
}
export interface GapItem {
  rel_path: string; symbol: string; kind: string; start_line: number; end_line: number; complexity: number; reasons: string[];
  has_associated_tests: boolean | null; associated_tests: string[];
  measured: { run_id: string; missing_line_count: number | null; missing_lines: number[]; note?: string } | null;
  evidence: "static" | "measured+static"; confidence: string; limitations: string;
}
export interface GapPage {
  items: GapItem[]; total: number; page: number; page_size: number; coverage_run_id: string | null; discovery_id: string | null; note: string;
}

// ---- Phase 06: failures, localization, investigations, verification ----
export type FailureStatus = "open" | "investigating" | "resolved" | "ignored";
export interface FailureItem {
  id: string; category: string; exception_type: string | null; test_id: string | null; summary: string; status: FailureStatus; source: string;
  occurrence_count: number; first_seen_at: string; last_seen_at: string; top_location: string | null;
  investigation_status: string | null; investigation_id: string | null;
}
export interface FailureFrame {
  idx: number; display_path: string; rel_path: string | null; function: string | null; function_source: string | null; line: number | null;
  col: number | null; scope: string; is_test: boolean; symbol_stable_id: string | null;
}
export interface FailureDetail extends FailureItem {
  message: string; fingerprint: string; warnings: string[]; frames_truncated: boolean; revision: string | null; revision_known: boolean;
  index_snapshot_id: string | null; frames: FailureFrame[]; occurrences: Record<string, unknown>[];
  related_findings: { id: string; rule_id: string; title: string; severity: string; rel_path: string; start_line: number | null }[];
  coverage: Record<string, unknown>; freshness: Record<string, unknown>; localization_meta: Record<string, unknown>;
}
export interface FailureIngestResult { items: FailureItem[]; created: number; recurred: number }
export interface EvidenceItem { id: string; type: string; weight: number; detail: string }
export interface LocalizationCandidate {
  rank: number; rel_path: string; symbol: string | null; symbol_stable_id: string | null; start_line: number | null; end_line: number | null;
  failing_line: number | null; score: number; confidence: "high" | "medium" | "low"; role: string; evidence: EvidenceItem[];
  contradicting: EvidenceItem[]; explanation: string;
}
export interface LocalizationOut {
  failure_id: string; state: "ranked" | "insufficient_evidence"; analysis_version: string; candidates: LocalizationCandidate[];
  missing_evidence: string[]; notes: string[]; stale: boolean; limitations: string[]; weights: Record<string, number>; interpretation: string;
}
export interface HypothesisRef { text: string; ref: string }
export interface Hypothesis {
  id: string; kind: "hypothesis"; status: string; statement: string; supporting: HypothesisRef[]; contradicting: HypothesisRef[]; verification: string[];
}
export interface ImpactOut {
  rel_path: string; max_depth: number; graph_available: boolean; truncated: boolean; limitations: string[];
  dependents: { rel_path: string; depth: number; direct: boolean; relationship: string }[];
  callers: Record<string, unknown>[]; related_tests: { rel_path: string }[];
}
export interface InvestigationContent {
  summary: string; observations: { text: string; ref: string; kind: "fact" }[]; candidates: Record<string, unknown>[]; localization_state: string;
  hypotheses: Hypothesis[]; missing_information: string[]; verification_steps: string[]; next_tests: string[];
  related_findings: { id: string; rule_id: string; title: string; severity: string; rel_path: string; start_line: number | null }[];
  related_failures: Record<string, unknown>[]; impact: ImpactOut | null; limitations: string[];
}
export interface RunSummary {
  id: string; kind: string; status: string; reason: string | null; exit_code: number | null; tests_collected: number | null;
  tests_passed: number | null; tests_failed: number | null; tests_errors: number | null; tests_skipped: number | null;
  finished_at: string | null; failures: { name: string | null; message: string | null }[];
}
export type VerificationOutcome = "PENDING" | "PERSISTS" | "NOT_REPRODUCED_IN_VERIFICATION_RUN" | "CHANGED_FAILURE" | "INCONCLUSIVE";
export interface VerificationItem {
  id: string; outcome: VerificationOutcome; meaning: string; reason: string | null; test_ids: string[]; created_at: string;
  evaluated_at: string | null; original_run: RunSummary | null; verification_run: RunSummary | null; baseline_available: boolean;
}
export interface InvestigationItem {
  id: string; failure_id: string; status: "open" | "in_review" | "closed"; resolution: string | null; generator_version: string;
  index_snapshot_id: string | null; created_at: string; updated_at: string; failure_summary: string | null;
}
export interface InvestigationDetail extends InvestigationItem { content: InvestigationContent; notes: string; verifications: VerificationItem[] }
export interface VerificationPreview {
  executes: false; requires: string; targets: string[]; default_targets: string[]; coverage: boolean; execution: string;
  baseline_execution_run_id: string | null; comparison: string; investigation_id: string;
}
export interface RelatedOut {
  failure_id: string; note: string; impact: ImpactOut | null;
  related_failures: { failure: FailureItem; shared_files: string[]; shared_symbol_count: number; relationship: string }[];
}

// ---- Phase 07: code review ------------------------------------------------------------------------------------------
export type ReviewMode = "revisions" | "staged" | "unstaged" | "working_tree";
export type ReviewStatus = "pending" | "running" | "completed" | "completed_with_warnings" | "failed";
export type ReviewFindingStatus = "open" | "accepted" | "dismissed" | "follow_up";
export interface ReviewItem {
  id: string; mode: ReviewMode; status: ReviewStatus; base_ref: string | null; head_ref: string | null; base_sha: string | null; head_sha: string | null;
  git_head: string | null; selected_paths: string[]; index_snapshot_id: string | null; index_stale: boolean; index_notes: string[];
  files_total: number; files_analyzed: number; files_omitted: number; lines_added: number; lines_removed: number; symbols_changed: number;
  findings_total: number; risk_state: string | null; risk_score: number | null; risk_category: string | null; warnings: string[];
  error_message: string | null; generator_version: string; created_at: string; started_at: string | null; finished_at: string | null;
}
export interface ReviewStage { name: string; status: string; detail?: string; duration_ms?: number }
export interface ReviewSummaryOut {
  review: ReviewItem; ready: boolean; stages: ReviewStage[];
  summary: {
    headline?: string; by_severity?: Record<string, number>; by_category?: Record<string, number>; clean_review_note?: string | null;
    top_risk_factors?: { id: string; label: string; contribution: number }[]; checks_run?: string[];
    noise_control?: Record<string, number>; ai?: { used: boolean; note: string }; findings_resolved_by_change?: number;
  };
}
export interface ReviewPreview {
  executes: boolean; mode: ReviewMode; total_files: number; omitted: number; explanation: string; warnings: string[];
  limits: { max_files: number; max_diff_bytes: number };
  files: { rel_path: string; old_path: string | null; change_type: string; added: number | null; removed: number | null; binary: boolean; language: string; is_test: boolean }[];
}
export interface ReviewChangedSymbol {
  qualified_name: string; symbol_stable_id: string | null; kind: string; start_line: number | null; end_line: number | null; change_kind: string;
  is_public: boolean; signature_old: string | null; signature_new: string | null; mapping: string; complexity: number | null;
}
export interface ReviewChange {
  id: string; rel_path: string; change_type: string; old_path: string | null; new_path: string | null; language: string; is_test: boolean; is_binary: boolean;
  lines_added: number | null; lines_removed: number | null; new_ranges: number[][]; extraction_status: string; notes: string[]; symbols: ReviewChangedSymbol[];
}
export interface ReviewFinding {
  id: string; stable_id: string; fingerprint: string; review_id: string; rule_id: string; rel_path: string; start_line: number | null; end_line: number | null;
  severity: string; category: string; title: string; explanation: string; evidence: string; remediation: string; limitations: string; confidence: string;
  actionable: boolean; source: string; ai_assisted: boolean; status: ReviewFindingStatus; rationale: string | null; inherited_from_review_id: string | null;
  related: Record<string, unknown>;
}
export interface ReviewRiskFactor {
  id: string; label: string; status: "measured" | "unavailable"; weight: number; raw: number | null; normalized: number | null; contribution: number | null;
  detail: string; evidence: string[];
}
export interface ReviewRisk {
  ready: boolean; status: ReviewStatus; state: string; score: number | null; category: string | null; reason?: string; factors: ReviewRiskFactor[];
  formula: string; disclaimer: string; thresholds: [string, number][] | Record<string, number>; evidence_share: number | null; notes: string[];
}
export interface ReviewImpact {
  ready: boolean; index_stale: boolean; index_notes: string[]; graph_available: boolean; complete: boolean; limitations: string[]; max_depth: number;
  dependents_total: number; dependents_incomplete: boolean;
  dependents: { rel_path: string; depth: number; relationship: string }[];
  affected_tests: { rel_path: string; relationship: string; note: string }[];
  failures?: { id: string; summary: string; status: string }[];
}
export interface ReviewRecommendation {
  target: { rel_path: string; symbol: string | null }; behavior: string; reason: string; evidence: string; evidence_source: string; priority: string; confidence: string;
  status: string; note: string; existing_tests: { status: string; tests?: string[]; note?: string };
}
export interface ReviewRecommendations { items: ReviewRecommendation[]; total: number; note: string; ready: boolean }

// ---------------------------------------------------------------------------------------------- Phase 08: repair
export type RepairStatus = "open" | "analyzed" | "proposed" | "patched" | "applied" | "verified" | "verification_failed" | "closed";
export type PatchStatus = "draft" | "validated" | "invalid" | "awaiting_approval" | "approved" | "rejected" | "applying" | "applied" | "verification_failed" | "verified";
export type RepairOutcome = "PENDING" | "PASSED" | "FAILED" | "INCOMPLETE" | "BLOCKED";
export interface RepairRootCause {
  state?: string; kind?: string; rel_path?: string; symbol?: string | null; confidence?: string; score?: number; summary?: string;
  confidence_rationale?: string; note?: string;
}
export interface RepairItem {
  id: string; title: string; description: string; severity: string; status: RepairStatus; source_kind: string;
  failure_id: string | null; finding_id: string | null; review_finding_id: string | null; debug_investigation_id: string | null;
  hint_files: string[]; relevant_files: string[]; root_cause: RepairRootCause; verification_outcome: RepairOutcome | null;
  evidence_assembled_at: string | null; proposal_generated_at: string | null; created_at: string; updated_at: string; closed_at: string | null;
  patch_count: number; latest_patch_status: PatchStatus | null;
}
export interface RepairFact { id: string; text: string; source: string; ref: string; basis: string }
export interface RepairEvidenceItem { id: string; type: string; weight: number; detail: string; ref?: string }
export interface RepairHypothesis {
  rank: number; rel_path: string; symbol: string | null; start_line: number | null; end_line: number | null; score: number; confidence: string;
  kind: string; status: string; rationale: string; evidence: RepairEvidenceItem[]; contradicting: string[];
}
export interface RepairEvidence {
  state?: string; facts?: RepairFact[]; hypotheses?: RepairHypothesis[]; unknowns?: string[]; next_steps?: string[]; limitations?: string[];
  weights?: Record<string, number | string>; min_score?: number; similar_failures?: unknown[];
}
export interface RepairTestTarget { target: string; reason: string; kind: string }
export interface RepairCandidateFix { rule_id: string; rel_path: string; line: number; scope: string; summary: string; strategy: string; expected_behavior: string; side_effects: string[] }
export interface RepairProposal {
  state?: string; ai_assisted?: boolean; problem_statement?: string; strategy?: string; expected_behavior?: string; side_effects?: string[];
  regression_risks?: string[]; tests_to_run?: RepairTestTarget[]; confidence?: { level: string; rationale: string }; patch_available?: boolean;
  patch_reason?: string; candidate_fixes?: RepairCandidateFix[]; limitations?: string[]; unresolved_questions?: string[];
  files?: { rel_path: string; symbol: string | null }[]; diagnostic_actions?: string[]; recommendation?: string;
}
export interface RepairDetail extends RepairItem { evidence: RepairEvidence; proposal: RepairProposal; baseline_execution_run_id: string | null; index_snapshot_id: string | null }
export interface PatchFileSummary { path: string; change_type: string; added: number; removed: number }
export interface PatchValidation {
  ok?: boolean; checked_at?: string; errors?: { code: string; message: string; details?: unknown }[]; warnings?: string[]; stale?: boolean;
  working_tree_modified?: boolean; checks_passed?: string[]; static_check?: { available: boolean; new_findings: { rel_path: string; rule_id: string }[]; resolved_findings: number; note?: string };
}
export interface PatchHunk { header: string; lines: { kind: "add" | "remove" | "context"; text: string }[] }
export interface PatchItem {
  id: string; investigation_id: string; source: string; rule_id: string | null; title: string; summary: string; status: PatchStatus; patch_sha256: string;
  files: PatchFileSummary[]; extra_files: string[]; validation: PatchValidation; validated_at: string | null; submitted_at: string | null;
  approved_by: string | null; approved_at: string | null; rejected_at: string | null; reject_reason: string | null; applied_at: string | null;
  apply_result: { ok?: boolean; message?: string; error?: string; instructions?: string[]; reverse_diff?: string }; verification_summary: Record<string, unknown>; created_at: string;
}
export interface PatchDetail extends PatchItem { diff_text: string; preview: { files?: (PatchFileSummary & { hunks: PatchHunk[] })[]; truncated?: boolean } }
export interface RepairCriterion { id: string; status: "pass" | "fail" | "incomplete" | "blocked" | "unavailable"; detail: string }
export interface RepairVerification {
  id: string; patch_id: string; investigation_id: string; execution_run_id: string | null; selected_tests: RepairTestTarget[]; outcome: RepairOutcome;
  outcome_reason: string; comparison: { baseline_available?: boolean; new_failures?: string[]; pre_existing_failures?: string[]; no_longer_failing?: string[]; failures_without_baseline_coverage?: string[]; note?: string };
  static_check: { available?: boolean; new_findings?: { rel_path: string; rule_id: string }[]; resolved_findings?: number };
  criteria: RepairCriterion[]; created_at: string; evaluated_at: string | null; meaning: string;
  run: { status: string; exit_code: number | null; command: string[]; started_at: string | null; finished_at: string | null; duration_ms: number | null; tests_passed: number | null; tests_failed: number | null;
    output_truncated: boolean; stdout_tail: string; stderr_tail: string; reason: string | null } | null;
}
export interface RepairVerifyPreview { executes: boolean; requires: string; patch_status: string; selected: RepairTestTarget[]; notes: string[]; execution: string }
export interface RepairRollback { originals_retained: boolean; applied: boolean; reverse_diff: string; instructions: string[]; current_matches_patched_result: boolean }
export interface RepairAuditEntry { id: string; action: string; created_at: string; details: Record<string, unknown> }

// ------------------------------------------------------------------------------------------------ refactoring (Phase 09)
/** Engine-specific JSON whose shape depends on the refactoring type; fields are read defensively. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Loose = Record<string, any>;
export type RefactorStatus = "planned" | "blocked" | "generated" | "invalid" | "validated" | "awaiting_approval" | "approved" | "rejected" | "applying" | "applied"
  | "verification_failed" | "verified" | "analyzed" | "closed";
export type RefactorSupport = "applicable" | "proposal_only" | "unsupported";
export interface RefactorInput { name: string; type: string; required: boolean; description: string }
export interface RefactorType {
  id: string; title: string; support: RefactorSupport; mode: "transform" | "analysis"; languages: string[]; inputs: RefactorInput[]; preconditions: string[]; scope: string;
  semantic_requirement: string; edit_types: string[]; validation: string[]; verification: string[]; limitations: string[]; reason: string; category: string;
}
export interface RefactorCatalog { types: RefactorType[]; support_levels: Record<string, string> }
export interface RefactorFinding { severity: "blocker" | "warning" | "info"; code: string; message: string; locations: { path: string; line: number; col?: number }[] }
export interface RefactorFile { path: string; change_type: string; base_sha256: string | null; result_sha256: string | null; added: number; removed: number }
export interface RefactorEdit {
  id?: string; rel_path: string; line?: number; start_line?: number; end_line?: number; col?: number; old: string; new: string; kind: string; confidence?: string; note?: string; context?: string;
}
export interface RefactorPlan {
  version: string; id: string; type: string; support: string; snapshot: { id: string | null; index_state: string; complete: boolean; notes: string[] };
  target: Record<string, unknown>; files: RefactorFile[]; edits: RefactorEdit[]; edit_count: number; findings: RefactorFinding[]; conflicts: RefactorFinding[]; side_effects: string[];
  limitations: string[]; acknowledgements_required: string[]; candidate_edits: RefactorEdit[]; preconditions: string[]; postconditions: string[];
  verification_strategy: { tests: string; proves: string; does_not_prove: string }; extra: Loose;
}
export interface RefactorImpact {
  changed_files?: string[]; index_complete?: boolean; snapshot_id?: string | null; caveats?: string[]; direct_dependents?: string[]; transitive_dependents?: string[];
  dependent_summary?: { direct: number; transitive: number; max_depth: number; truncated: boolean; more_beyond_depth: boolean };
  cycles?: { existing_cycles_involving_changed_files?: string[][]; edge_set_changed_in?: string[]; new_cycles?: string[][] | null; proof?: string };
  public_entry_points?: { rel_path: string; symbol: string | null; kind: string; line: number; reason: string }[];
  affected_tests?: { discovered: boolean; associated: Record<string, string[]>; basis?: string }; error?: string;
}
export interface RefactorItem {
  id: string; repository_id: string; type_id: string; title: string; status: RefactorStatus; support_level: RefactorSupport; index_snapshot_id: string | null;
  target: Record<string, unknown>; patch_sha256: string | null; files: RefactorFile[]; verification_summary: { outcome?: string; reason?: string };
  created_by: string | null; created_at: string; updated_at: string; validated_at: string | null; approved_at: string | null; applied_at: string | null; blockers: number; warnings: number;
}
export interface RefactorCheck { id: string; status: string; detail: string }
export interface RefactorDetail extends RefactorItem {
  params: Record<string, unknown>; plan: RefactorPlan; impact: RefactorImpact; apply_result: Loose; reject_reason: string | null; submitted_at: string | null;
  approved_by: string | null; rejected_at: string | null; applied_by: string | null; baseline_execution_run_id: string | null; baseline_note: string | null; has_diff: boolean;
  validation: { ok?: boolean; stale?: boolean; errors?: { code: string; message: string }[]; warnings?: string[]; checks?: RefactorCheck[]; not_checked?: string[];
    static_check?: { available: boolean; new_findings: unknown[]; resolved_findings: number } };
}
export interface RefactorDiff {
  id: string; status: string; has_diff: boolean; reason?: string; patch_sha256?: string;
  preview: { files: { path: string; change_type: string; added: number; removed: number; hunks: { header: string; lines: { kind: "add" | "remove" | "context"; text: string }[] }[] }[]; truncated?: boolean; error?: string };
}
export interface DuplicateMember { rel_path: string; name: string; kind: string; start_line: number; end_line: number; lines: number; tokens: number; excerpt: string }
export interface DuplicateGroup {
  id: string; kind: "exact" | "renamed" | "structural"; lines_total: number; similarity: number; average_similarity: number; similarity_meaning: string; members: DuplicateMember[];
  shared: { token_count_range: number[]; shared_identifiers: string[]; shared_calls: string[] }; differences: { behavioral: boolean; summary: string };
  strategies: { id: string; summary: string; caution: string }[]; limitations: string[]; auto_merge: false; pairs: { a: string; b: string; kind: string; similarity: number }[];
}
export interface DuplicatePage {
  id: string; total: number; page: number; page_size: number; groups: DuplicateGroup[]; stats: Loose; filters: Record<string, unknown>; limitations: string[];
  index: { complete?: boolean };
}
export interface RefactorVerification {
  id: string; transformation_id: string; execution_run_id: string | null; baseline_execution_run_id: string | null; selected_tests: RepairTestTarget[]; outcome: RepairOutcome;
  outcome_reason: string; meaning: string; comparison: Loose; static_check: Loose; criteria: { id: string; status: string; detail: string }[];
  created_at: string; evaluated_at: string | null;
  run: RepairVerification["run"];
}
export interface RefactorVerifyPreview extends Omit<RepairVerifyPreview, "patch_status"> { status: string; baseline_execution_run_id: string | null; baseline_note: string; not_proven: string }
export interface RefactorRollback { originals_retained: boolean; applied: boolean; reverse_diff: string; instructions: string[]; current_matches_refactored_result: boolean | null }
export interface RefactorRenameRequest { type_id: string; rel_path: string; name: string; new_name: string; function?: string; class_name?: string; kind?: string }
export interface RefactorDuplicatesRequest { min_lines?: number; threshold?: number; path_prefix?: string; exclude_tests?: boolean; exclude_paths?: string[] }

// ---- Continuous verification (Phase 10) ----
export type GateOutcome = "PASS" | "FAIL" | "BLOCKED" | "INCOMPLETE";
export type CvRunStatus = "queued" | "running" | "completed" | "cancelled" | "error";
export type CvSource = "working_tree" | "staged" | "revisions" | "patch" | "manifest";
export type CvMode = "focused" | "standard" | "full";
export interface CvFreshness { stale: boolean | null; reason: string | null; current_head?: string | null }
export interface CvRunSummary {
  id: string; kind: "verify" | "baseline"; status: CvRunStatus; mode: CvMode; source: CvSource; head_sha: string | null; base_sha: string | null;
  tree_fingerprint: string | null; policy_version: number; baseline_id: string | null; execution_run_id: string | null; gate_outcome: GateOutcome | null; reason: string | null;
  cancel_requested: boolean; files_changed: number; link: Loose; requested_by: string | null; created_at: string; started_at: string | null; finished_at: string | null;
}
export interface CvCheck {
  id: string; check_id: string; tier: number; required: boolean; reason: string; confidence: string; prerequisites: string[];
  status: "planned" | "running" | "passed" | "failed" | "blocked" | "incomplete" | "error" | "cancelled" | "not_applicable"; result: Loose; started_at: string | null; finished_at: string | null;
}
export interface CvGateRule { id: string; status: "pass" | "fail" | "blocked" | "incomplete" | "n/a"; required: boolean; detail: string; evidence: Loose }
export interface CvGate { outcome: GateOutcome | null; reason: string; precedence: GateOutcome[]; rules: CvGateRule[]; meaning?: string }
export interface CvRegressionItem { test: string; category: "new" | "pre_existing" | "flaky" | "unclassified" | "resolved"; explanation: string; message?: string; evidence: Loose }
export interface CvRegression {
  available: boolean; failing_tests?: string[]; scope?: Loose; baseline?: { compatible: boolean; problems: string[]; baseline_id: string | null; head_sha?: string | null; recorded_at?: string | null };
  items?: CvRegressionItem[]; counts?: Record<"new" | "pre_existing" | "flaky" | "unclassified" | "resolved", number>; basis?: string; reason?: string;
}
export interface CvChangedFile { path: string; status: string; old_path: string | null; sha256: string | null; notes: string[] }
export interface CvChangeSet { source: CvSource; base_sha: string | null; head_sha: string | null; git_head: string | null; fingerprint: string; warnings: string[]; files: CvChangedFile[]; counts: { total: number; python: number; docs: number } }
export interface CvPlannedCheck { check_id: string; tier: number; required: boolean; reason: string; confidence: string; prerequisites: string[]; status: string; result?: Loose }
export interface CvPlan { mode: CvMode; mode_meaning: string; checks: CvPlannedCheck[]; test_targets: string[] | null; unverified_paths: string[]; notes: string[]; binding_problems: string[]; impact: Loose; baseline?: { id: string | null; selection: string } }
export interface CvPreview { executes: false; change_set: CvChangeSet; plan: CvPlan }
export interface CvRunDetail extends CvRunSummary {
  change_set: CvChangeSet; plan: CvPlan; checks: CvCheck[]; policy_snapshot: Loose; gate: CvGate | null; regression: CvRegression | null; coverage: Loose | null; static: Loose | null; freshness: CvFreshness;
}
export interface CvBaseline {
  id: string; label: string; status: string; run_id: string | null; execution_run_id: string | null; head_sha: string | null; tree_fingerprint: string; scope: Loose;
  failing: string[]; totals: Record<string, number>; coverage: Loose; integrity_ok: boolean; created_by: string | null; created_at: string;
}
export interface CvPolicy { version: number; is_default: boolean; rules: Loose; can_edit: boolean; precedence: GateOutcome[] }
export interface CvOverview {
  policy_version: number; policy_is_default: boolean; policy: Loose; latest_run: CvRunSummary | null; latest_baseline: CvBaseline | null; active_run: CvRunSummary | null;
  gate_counts: Record<GateOutcome, number>;
}
export interface CvEvent { id: string; kind: string; detail: Loose; created_at: string }
export interface CvCompare { run_id: string; baseline_id: string; persisted: boolean; compatibility: { compatible: boolean; problems: string[] }; regression: CvRegression; coverage: Loose; note: string }
export interface CvGateView { run_id: string; status: CvRunStatus; outcome: GateOutcome | null; policy_version: number; policy_snapshot: Loose; decision: CvGate; freshness: CvFreshness }
export interface CvStartBody { source: CvSource; mode?: CvMode; base?: string; head?: string; paths?: string[]; patch?: string; coverage?: boolean; baseline_id?: string; confirm: boolean }

// ---- Phase 11: CI/CD, change requests, release readiness ----
export type CiReadiness = "READY" | "NOT_READY" | "BLOCKED" | "INCOMPLETE";
export interface CiConnection {
  id: string; provider: "local" | "github"; external_repo: string; default_branch: string; enabled: boolean; enabled_events: string[]; status: string; status_context: string;
  token_env: string; token_configured: boolean; webhook_secret_env: string; webhook_secret_configured: boolean;
  capabilities: { webhooks: boolean; status_reporting: boolean; file_listing: boolean; local_branches: boolean };
  last_validated_at: string | null; last_sync_at: string | null; last_error: string; created_at: string; updated_at: string;
}
export interface CiOverview { connections: CiConnection[]; change_requests: { open: number; closed: number; merged: number }; readiness_policy_version: number; readiness_policy_is_default: boolean; can_admin: boolean }
export interface CiVerificationSummary { latest_run_id: string | null; latest_head_sha: string | null; current_revision_verified: boolean; stale: boolean; gate_outcome: GateOutcome | null; readiness: CiReadiness | null }
export interface CiChangeRequest {
  id: string; provider: string; external_id: string; source_branch: string; target_branch: string; head_sha: string; base_sha: string | null; title: string; author: string; state: string;
  files_count: number; files_source: string; files_truncated: boolean; head_changed_at: string | null; updated_at: string; verification: CiVerificationSummary;
  description?: string; changed_files?: { path: string; status: string }[]; notes?: string[]; connection_id?: string | null;
}
export interface CiWorkflowRun {
  id: string; change_request_id: string; head_sha: string; base_sha: string | null; mode: string; trigger: string; status: string; cv_run_id: string | null; gate_outcome: GateOutcome | null;
  readiness: CiReadiness | null; readiness_effective: CiReadiness | null; stale: boolean; current_head_sha: string | null; readiness_policy_version: number | null; evaluated_at: string | null;
  error: string | null; requested_by: string | null; created_at: string; finished_at: string | null; reused?: boolean;
  checks?: { check_id: string; status: string; required: boolean; reason: string }[];
}
export interface CiReadinessRule { id: string; status: string; required: boolean; detail: string }
export interface CiReadinessResult {
  workflow_run_id: string; outcome: CiReadiness | null; stale: boolean; reason?: string;
  decision: { outcome: CiReadiness; reason: string; revision: string; meaning: string; precedence: string[]; rules: CiReadinessRule[] } | null;
}
export interface CiStatusReport { id: string; workflow_run_id: string; provider: string; head_sha: string; context: string; state: string; description: string; status: string; attempts: number; error_class: string | null; error: string | null; retryable: boolean; next_retry_at: string | null; created_at: string }
export interface CiReportSummary { id: string; kind: "review" | "release"; change_request_id: string; workflow_run_id: string | null; head_sha: string; sha256: string; size_bytes: number; truncated: boolean; created_at: string }
export interface CiReportFull extends CiReportSummary { body: CiReviewReport; markdown: string }
export interface CiReviewReport {
  schema: string; kind: string; repository: { id: string; name: string };
  revision: { head_sha: string; base_sha: string | null; evidence_head_sha: string | null; evidence_is_current: boolean };
  change_request: { id: string; provider: string; external_id: string; source_branch: string; target_branch: string; title: string; author: string; state: string; files_source: string };
  changed_files: { path: string; status: string }[];
  checks: { check_id: string; required: boolean; status: string; detail: string }[];
  quality_gate: { outcome: GateOutcome; reason: string } | null;
  findings: { rule_id: string; title: string; severity: string; location: { path: string }; remediation: string }[];
  security_analysis: { performed: boolean; note: string; scan_id?: string; state?: string };
  regressions: { available: boolean | null; counts: Record<string, number> | null; items: { test: string; category: string }[] };
  release_readiness: { outcome: CiReadiness; recorded_outcome: CiReadiness; stale: boolean; decision: CiReadinessResult["decision"] } | null;
  gaps: string[]; limitations: string[];
}
export interface CiReadinessPolicy { version: number; is_default: boolean; rules: Record<string, unknown>; precedence: string[]; can_edit: boolean }

// ------------------------------------------------------------------------------------------------ security intelligence (Phase 12)
export type SecSeverity = "critical" | "high" | "medium" | "low" | "info";
export type SecStatus = "open" | "resolved" | "false_positive" | "accepted_risk" | "suppressed";
export interface SecPriority { bucket: "P1" | "P2" | "P3" | "P4"; score: number; model: string; factors?: string[]; floored?: boolean }
export interface SecScan {
  id: string; status: "queued" | "running" | "completed" | "failed"; scan_types: string[]; options: { exclude_globs?: string[]; include_dev?: boolean }; head_sha: string | null; dirty: boolean;
  error: string | null; created_at: string; started_at: string | null; finished_at: string | null; inventory_digest: string | null; source_digest: string | null;
  summary: { state?: "complete" | "partial" | "failed"; incomplete_reasons?: string[]; headline?: string; findings_total?: number; open_by_severity?: Record<string, number>; components?: number; suppressions_applied?: Record<string, number> };
  coverage?: Record<string, unknown>; tool_versions?: Record<string, string>;
}
export interface SecFinding {
  id: string; kind: "code" | "secret" | "dependency"; rule_id: string; category: string; title: string; severity: SecSeverity; confidence: string; evidence_quality: string; rel_path: string | null; line: number | null;
  status: SecStatus; priority: SecPriority; component_key: string | null; first_seen_at: string; last_seen_at: string; match_status: string | null; preview: string | null;
}
export interface SecEventItem { action: string; from_status: string | null; to_status: string | null; reason: string; actor_user_id: string | null; created_at: string }
export interface SecFindingDetail extends SecFinding {
  description: string; remediation: string; references: string[]; resolution_reason: string | null; suppression_id: string | null; events: SecEventItem[];
  evidence: { assessment?: string; sources?: { provider: string; advisory_id: string; url: string | null; retrieved_at: string | null }[]; fixed_versions?: string[]; advisory_ids?: string[]; reason?: string; severity_source?: string; reachability?: string; paths?: string[][]; detail?: string; length?: number };
}
export interface SecComponent {
  id: string; ecosystem: string; name: string; version: string | null; version_state: string; relationship: string; scope: string; manifests: { path: string }[]; declared: string[]; paths: string[][]; license: string | null; notes: string[];
  assessment: { status: string; reason?: string; sources?: string[] } | null;
}
export interface SecSuppression { id: string; kind: string; fingerprint: string | null; rule_id: string | null; path_glob: string | null; reason: string; created_at: string; expires_at: string | null; state: "active" | "expired" | "revoked" }
export interface SecException { id: string; selector: { type: string; value: string }; reason: string; expires_at: string; state: "active" | "expired" | "revoked"; created_at: string }
export interface SecProvider { name: string; enabled: boolean; kind: string; detail: string }
export interface SecEvaluation {
  evaluation_id?: string; policy_id: string; policy_version: number; status: "pass" | "fail" | "indeterminate"; explanation: string; evaluated_at: string; missing_data: string[]; skipped_checks: string[];
  checks: { id: string; status: string; detail: string }[]; causes: { finding_id: string; title: string; severity: string; why: string }[];
  exceptions_applied: { exception_id: string; finding_id: string; reason: string; expires_at: string }[]; expired_exceptions: { exception_id: string; finding_id: string }[]; revision: { head_sha: string | null; dirty: boolean }; meaning: string;
}
export interface SecOverview {
  latest_scan: SecScan | null; latest_completed_scan: SecScan | null; findings_by_status: Record<SecStatus, number>; open_by_severity: Record<SecSeverity, number>;
  latest_evaluation: { id: string; status: "pass" | "fail" | "indeterminate"; policy_version: number; scan_id: string; created_at: string; explanation: string } | null;
  policy_version: number; policy_is_default: boolean; providers: SecProvider[]; can_write: boolean; can_admin: boolean; notice: string | null;
}
export interface SecPolicyView { version: number; is_default: boolean; rules: Record<string, unknown>; can_edit: boolean; priority_model: string }
export interface SecRemediation {
  id: string; finding_id: string; kind: "dependency" | "code"; status: "proposed" | "cannot_fix" | "validated" | "validation_failed" | "submitted" | "rejected"; investigation_id: string | null; patch_id: string | null;
  proposal: { supported: boolean; reason?: string; change?: string; caveats?: string[]; side_effects?: string[]; to_version?: string; from_version?: string; major_version_change?: boolean; manifest?: string; basis?: string; repair_patch_status?: string };
  diff_text?: string; validation?: { ok?: boolean; note?: string; files_modified?: boolean };
}
export interface SecReportSummary { id: string; scan_id: string; format: string; sha256: string; size_bytes: number; truncated: boolean; created_at: string; body?: string }

// ---- Operational intelligence (Phase 13) ----
export interface OpsDependency { name: string; critical: boolean; state: string; latency_ms?: number; error_class?: string | null; detail?: string; probed?: boolean }
export interface OpsHealth { status: string; critical_unavailable: string[]; degraded: string[]; dependencies: OpsDependency[]; cached: boolean; checked_at: number }
export interface OpsOverview {
  health: { status: string; critical_unavailable: string[]; degraded: string[] };
  resources: { uptime_seconds: number; cpu_seconds: number; memory_bytes: number | null; threads: number };
  observed_since: string;
  requests_last_hour: { total: number; errors_5xx: number; p95_seconds: number | null };
  jobs_last_hour: { total: number; failed: number; in_flight: number };
  workflows_last_hour: Record<string, Record<string, number>>;
  open_incidents: number;
  slos: { slo_id: string; status: string; sli_value: number | null; target: number }[];
  notifications: { enabled: boolean; channels: { channel: string; enabled: boolean; implemented?: boolean; note?: string }[] };
  periodic_evaluation: { enabled: boolean; running?: boolean; last_run?: string | null };
  limitations: string[];
}
export interface OpsSignal { signal: string; status: string; exported: string; limits: string }
export interface OpsIncident {
  id: string; fingerprint: string; source: string; rule_id: string | null; rule_version: number | null; severity: "info" | "warning" | "critical"; component: string; title: string; summary: string;
  status: "open" | "acknowledged" | "investigating" | "resolved" | "reopened"; occurrences: number; evidence: Record<string, unknown>; first_seen_at: string; last_seen_at: string;
  acknowledged_at: string | null; resolved_at: string | null; resolution_note: string | null;
}
export interface OpsEventRec { id: string; event_type: string; summary: string; occurred_at: string; note: string | null; severity: string }
export interface OpsDeliveryRec { id: string; channel: string; destination: string; attempt: number; status: string; http_status: number | null; error_class: string | null; created_at: string }
export interface OpsIncidentDetail extends OpsIncident { history: OpsEventRec[]; deliveries: OpsDeliveryRec[] }
export interface OpsAlertEval {
  rule_id: string; rule_version: number; description: string; metric: string; window_minutes: number; comparator: string; threshold: number; min_samples: number; severity: string;
  status: "firing" | "ok" | "no_data" | "disabled"; fired: boolean; observed: number | null; samples: number; missing_data: boolean; explanation: string;
  incident: { id: string; transition: string; status: string } | null;
}
export interface OpsSlo {
  slo_id: string; config_version: number; description: string; sli: string; target: number; window_minutes: number; effective_window_minutes: number; window_fully_observed: boolean;
  status: "ok" | "at_risk" | "exhausted" | "insufficient_data" | "no_data"; good: number; total: number; sli_value: number | null; burn_rate: number | null; calculation: string; exclusions: string[];
  min_samples: number; limitations: string[]; observed_since: string; note?: string | null;
  error_budget: { fraction: number; allowed_bad_events: number; consumed_fraction: number | null; remaining_fraction: number } | null;
}
export interface OpsLogRecord { ts: string; level: string; logger: string; event?: string; message: string; request_id?: string; trace_id?: string; job_id?: string; error_class?: string; duration_ms?: number }
export interface OpsSpan { trace_id: string; span_id: string; parent_id: string | null; name: string; start: string; duration_ms: number; status: string; attributes: Record<string, unknown>; request_id: string | null }
export interface OpsDiagnostic {
  rule: string; title: string; severity: string; confidence: string; window_minutes: number; signals: { name: string; value: unknown }[]; evidence_refs: { type: string; ref: string }[];
  relationship: string; alternatives: string[]; investigation_steps: string[]; causal_claim: boolean;
}
