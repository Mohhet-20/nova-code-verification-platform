"""Deterministic baseline planner.

This is NOT an AI agent. It tokenizes the task text, ranks indexed file *paths* by keyword
overlap, detects project manifests from the inventory and assembles a structured draft.
It reads no file contents, executes nothing and modifies nothing. The same input always
produces the same output.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.schemas.tasks import ExpectedCheck, PlanContent, PlanStep

GENERATOR = "deterministic-baseline"
GENERATOR_VERSION = "1"

_STOPWORDS = frozenset("""a an and are as at be by can for from has have in into is it its of on or
that the this to was were will with should must when then than so not no add make new use using
used need needs able allow allows support implement create update fix change changes feature
code file files task user users system service data
src lib app tests test docs main index""".split())

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT = re.compile(r"[^A-Za-z0-9]+")
_PATH_LIKE = re.compile(r"[\w.\-]+(?:/[\w.\-]+)+")


@dataclass(frozen=True)
class InventoryFile:
    rel_path: str
    language: str


@dataclass(frozen=True)
class PlannerInput:
    title: str
    problem_statement: str
    objective: str
    scope: Sequence[str]
    acceptance_criteria: Sequence[str]
    files: Sequence[InventoryFile]
    indexed: bool


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for chunk in _SPLIT.split(_CAMEL.sub(" ", text)):
        low = chunk.lower()
        if len(low) >= 3 and low not in _STOPWORDS and not low.isdigit():
            tokens.append(low)
    return tokens


def _is_test_path(path: str) -> bool:
    low = path.lower()
    name = low.rsplit("/", 1)[-1]
    return ("/tests/" in f"/{low}" or "/test/" in f"/{low}" or "/__tests__/" in f"/{low}"
            or name.startswith("test_") or ".test." in name or ".spec." in name
            or name.endswith("_test.go") or name.endswith("_test.py"))


def rank_files(keywords: dict[str, int], explicit: set[str],
               files: Sequence[InventoryFile]) -> list[tuple[int, str]]:
    scored: list[tuple[int, str]] = []
    for f in files:
        parts = f.rel_path.split("/")
        base_tokens = set(tokenize(parts[-1]))
        dir_tokens = {t for p in parts[:-1] for t in tokenize(p)}
        score = 0
        for kw, weight in keywords.items():
            if kw in base_tokens:
                score += 3 * weight
            elif kw in dir_tokens:
                score += 1 * weight
        if f.rel_path in explicit:
            score += 20
        if score > 0:
            scored.append((score, f.rel_path))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return scored


def detect_checks(files: Sequence[InventoryFile]) -> list[ExpectedCheck]:
    root = {f.rel_path for f in files if "/" not in f.rel_path}
    any_paths = {f.rel_path for f in files}
    has_py_tests = any(
        f.language == "Python" and _is_test_path(f.rel_path) for f in files
    )
    checks: list[ExpectedCheck] = []

    def add(name: str, cmd: str | None, why: str) -> None:
        checks.append(ExpectedCheck(name=name, suggested_command=cmd, rationale=why))

    if "package.json" in root:
        note = " (confirm these scripts exist in package.json before use)"
        add("JavaScript/TypeScript tests", "npm test", "package.json found at the repository root" + note)
        add("Lint", "npm run lint", "package.json found at the repository root" + note)
        add("Build", "npm run build", "package.json found at the repository root" + note)
    if root & {"pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini"} or has_py_tests:
        add("Python tests", "python -m pytest", "Python project files or test files were found")
    if "go.mod" in root:
        add("Go tests", "go test ./...", "go.mod found at the repository root")
    if "Cargo.toml" in root:
        add("Rust tests", "cargo test", "Cargo.toml found at the repository root")
    if "pom.xml" in root:
        add("Maven tests", "mvn test", "pom.xml found at the repository root")
    if root & {"build.gradle", "build.gradle.kts"}:
        add("Gradle tests", "gradle test", "Gradle build file found at the repository root")
    if "Makefile" in root and not checks:
        add("Make target", "make test", "Makefile found; confirm a 'test' target exists")
    if any(p.startswith(".github/workflows/") for p in any_paths):
        add("CI parity review", None, "CI workflows exist; compare local checks with the CI definition")
    if not checks:
        add("Manual verification", None,
            "No known project manifest was detected; define verification commands manually.")
    return checks


def build_plan(inp: PlannerInput) -> PlanContent:
    text_parts = [inp.title, inp.problem_statement, inp.objective, *inp.scope,
                  *inp.acceptance_criteria]
    keywords: dict[str, int] = {}
    for weight, text in ((3, inp.title), (2, inp.objective), (1, inp.problem_statement)):
        for tok in tokenize(text):
            keywords[tok] = max(keywords.get(tok, 0), weight)
    for text in (*inp.scope, *inp.acceptance_criteria):
        for tok in tokenize(text):
            keywords.setdefault(tok, 1)

    known = {f.rel_path for f in inp.files}
    explicit = {m.rstrip('.,;:') for text in text_parts for m in _PATH_LIKE.findall(text)} & known

    ranked = rank_files(keywords, explicit, inp.files)
    source_hits = [p for _, p in ranked if not _is_test_path(p)][:8]
    test_hits = [p for _, p in ranked if _is_test_path(p)][:5]
    relevant = (source_hits + test_hits)[:12]

    warnings: list[str] = []
    assumptions = [
        "Relevance is estimated from file names and directory names only; file contents were not read.",
        "Suggested commands are inferred from manifest file names and have not been run.",
    ]
    if not inp.indexed:
        warnings.append("The repository has not been indexed, so no files could be matched. "
                        "Index it and regenerate the plan.")
        confidence = "low"
    elif not source_hits:
        warnings.append("No indexed file names matched the task keywords. Review the scope and "
                        "name concrete modules or paths in the task description.")
        confidence = "low"
    elif len(source_hits) >= 3 and ranked and ranked[0][0] >= 6:
        confidence = "medium"
    else:
        confidence = "low"
    if explicit:
        confidence = "medium" if confidence == "low" else "high"

    checks = detect_checks(inp.files) if inp.indexed else [
        ExpectedCheck(name="Manual verification", suggested_command=None,
                      rationale="Repository not indexed; verification commands could not be inferred.")
    ]

    scope_text = (
        "Check that each acceptance criterion is testable and that the explicit scope covers "
        "everything required: " + "; ".join(inp.scope[:5])[:1200]
        if inp.scope
        else "No explicit scope was provided; define what is in and out of scope."
    )
    steps: list[PlanStep] = [
        PlanStep(id="s1", kind="investigate", title="Review the candidate files",
                 description="Read the files below and confirm they are the right place for this "
                             "change. Adjust the list if the heuristic missed something.",
                 target_paths=source_hits[:8]),
        PlanStep(id="s2", kind="investigate", title="Confirm scope and acceptance criteria",
                 description=scope_text),
        PlanStep(id="s3", kind="implement", title="Implement the change",
                 description=f"Make the smallest change that achieves the objective: {inp.objective[:300]}",
                 target_paths=source_hits[:5], requires_approval=True),
        PlanStep(id="s4", kind="test", title="Add or update tests",
                 description=("Extend the existing tests listed below to cover each acceptance criterion."
                              if test_hits else
                              "No related test files were found; add tests that demonstrate each "
                              "acceptance criterion."),
                 target_paths=test_hits, requires_approval=True),
        PlanStep(id="s5", kind="verify", title="Run the verification checks",
                 description="Run the expected checks after a human approves each command. "
                             "NOVA does not execute commands in this version.",
                 requires_approval=True),
        PlanStep(id="s6", kind="review", title="Review the diff and record evidence",
                 description="Inspect the final diff, confirm every acceptance criterion with "
                             "evidence, and note anything left uncertain."),
    ]
    summary = (f"Baseline plan for '{inp.title}': {len(source_hits)} candidate source file(s) and "
               f"{len(test_hits)} related test file(s) identified by name matching; "
               f"{len(checks)} verification check(s) suggested. Generated deterministically; "
               "no files were read, changed or executed.")
    return PlanContent(summary=summary, confidence=confidence, relevant_files=relevant,
                       steps=steps, expected_checks=checks, assumptions=assumptions,
                       warnings=warnings)
