export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 10 ? 0 : 1)} ${units[i]}`;
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  // Backend timestamps are UTC; SQLite may return them without a zone suffix.
  const d = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

export const SKIP_LABELS: Record<string, string> = {
  IGNORED_PATTERN: "Ignored (excluded directory or .gitignore)",
  BINARY_FILE: "Binary files",
  FILE_TOO_LARGE: "Over size limit",
  UNSUPPORTED_ENCODING: "Not valid UTF-8 text",
  OUTSIDE_REPOSITORY: "Outside repository root",
  SYMLINK_REJECTED: "Symbolic links (never followed)",
  SECRET_LIKE_PATH: "Credential-like files (names not recorded)",
  UNSUPPORTED_FILE_TYPE: "Special files",
  UNREADABLE: "Unreadable",
  MAX_DEPTH_EXCEEDED: "Beyond depth limit",
  DUPLICATE_FILE: "Duplicate (hard link)",
};

export const ANALYSIS_LABELS: Record<string, string> = {
  parsed: "Parsed",
  partial: "Partial (syntax errors)",
  failed: "Analysis failed",
  unavailable: "Parser unavailable",
  unsupported: "Metadata only",
};

export const RESOLUTION_LABELS: Record<string, string> = {
  resolved: "Resolved to a repository file",
  external: "External package",
  unresolved: "Unresolved",
  unsupported: "Not resolvable by NOVA",
};

export const MATCH_LABELS: Record<string, string> = {
  exact_name: "exact name",
  exact_qualified_name: "exact qualified name",
  name_prefix: "name starts with",
  qualified_prefix: "qualified name starts with",
  name_substring: "name contains",
  qualified_substring: "qualified name contains",
};

/** A snapshot whose data queries can serve. */
export const isPublished = (status: string | null | undefined): boolean =>
  status === "completed" || status === "completed_with_warnings";

export function splitLines(text: string): string[] {
  return text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
}
