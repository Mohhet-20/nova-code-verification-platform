import type { Plan, RepositoryDetail, Snapshot, TaskDetail } from "../types/api";

export const snapshot: Snapshot = {
  id: "snap1234abcd", repository_id: "r1", status: "completed",
  started_at: "2026-10-18T10:00:00Z", finished_at: "2026-10-18T10:00:02Z",
  files_indexed: 42, directories_visited: 7, total_bytes: 2048, total_lines: 900,
  skipped_counts: { SECRET_LIKE_PATH: 2, BINARY_FILE: 3 }, language_counts: { Python: 30, Markdown: 12 },
  truncated: false, truncation_reason: null, error_message: null,
  limits: { max_files: 20000 }, git_head: "abcdef1234567890", git_branch: "main",
  generation: 1, mode: "incremental", files_new: 42, files_changed: 0, files_unchanged: 0, files_removed: 0,
  files_failed: 0, files_skipped: 5, symbols_count: 120, imports_total: 30, imports_unresolved: 4,
  relationships_count: 18, analysis_counts: { parsed: 30, unsupported: 12 }, warnings: [], duration_ms: 1500,
};

export const repoDetail: RepositoryDetail = {
  id: "r1", name: "payments", description: "", root_path: "/work/payments", root_label: "payments",
  is_git: true, git_branch: "main", git_head: "abcdef1234567890", git_dirty_count: 0,
  created_at: "2026-10-18T09:00:00Z", updated_at: "2026-10-18T09:00:00Z", latest_snapshot: snapshot,
};

export const plan: Plan = {
  generator: "deterministic-baseline", generator_version: "1", revision: 1, edited_by_user: false,
  snapshot_id: "snap1234abcd", updated_at: "2026-10-18T10:05:00Z",
  content: {
    summary: "Baseline plan: 2 candidate files.", confidence: "medium",
    relevant_files: ["src/auth/login.py"],
    steps: [{ id: "s1", kind: "investigate", title: "Review the candidate files", description: "Read them.", target_paths: ["src/auth/login.py"], requires_approval: false },
            { id: "s5", kind: "verify", title: "Run the verification checks", description: "After approval.", target_paths: [], requires_approval: true }],
    expected_checks: [{ name: "Python tests", suggested_command: "python -m pytest", rationale: "tests found", executed: false }],
    assumptions: ["Relevance is estimated from file names only."], warnings: [],
  },
};

export const task: TaskDetail = {
  id: "t1", repository_id: "r1", repository_name: "payments", title: "Harden login",
  problem_statement: "Login accepts any password.", objective: "Validate passwords.",
  scope: ["src/auth"], acceptance_criteria: ["Wrong password rejected"], status: "planned",
  created_at: "2026-10-18T10:05:00Z", updated_at: "2026-10-18T10:05:00Z", plan,
};
