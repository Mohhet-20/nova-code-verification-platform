"""Catalogue of refactoring types. A type is *applicable* (a validated diff can be produced and applied), *proposal_only*
(analysis or suggestions; never applied) or *unsupported* (listed so callers get an explicit answer instead of a silent no-op)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PY = ["python"]
JS = ["javascript", "jsx", "typescript", "tsx"]


@dataclass(frozen=True)
class RefactorType:
    id: str
    title: str
    support: str  # applicable | proposal_only | unsupported
    mode: str  # transform | analysis
    languages: list[str]
    inputs: list[dict[str, Any]]
    preconditions: list[str]
    scope: str
    semantic_requirement: str
    edit_types: list[str]
    validation: list[str]
    verification: list[str]
    limitations: list[str]
    reason: str = ""
    category: str = "rename"
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in ("id", "title", "support", "mode", "languages", "inputs", "preconditions", "scope", "semantic_requirement", "edit_types",
                                              "validation", "verification", "limitations", "reason", "category")}


def _in(name: str, typ: str, desc: str, required: bool = True) -> dict[str, Any]:
    return {"name": name, "type": typ, "required": required, "description": desc}


COMMON_VALIDATION = ["Source hashes match the plan (stale-source detection)", "No overlapping edits", "Paths stay inside the repository (no symlinks, allow-listed types)",
                     "Parse check of every changed file", "Undefined-name comparison before/after", "Static-analysis findings before/after"]
COMMON_VERIFICATION = ["Selected tests run before/after through the controlled runner", "Results compared against a baseline", "Outcome never claims behavioral equivalence"]

TYPES: list[RefactorType] = [
    RefactorType("python.rename_symbol", "Rename a module-level Python symbol", "applicable", "transform", PY,
                 [_in("rel_path", "string", "File defining the symbol"), _in("name", "string", "Current name"), _in("new_name", "string", "New name"), _in("kind", "string", "function | class | variable (optional disambiguation)", False)],
                 ["File is indexed and parses", "Exactly one module-level definition of the name in the file", "New name is a valid, unused identifier in every affected scope"],
                 "Module scope plus every importer (aliased imports, module attributes, wildcard imports, re-exports, __all__)",
                 "Scope analysis (stdlib ast) resolves every identifier; same-spelled names in other scopes are untouched; comments and strings are never edited",
                 ["identifier replacement", "import name replacement", "__all__ string entry replacement"], COMMON_VALIDATION + ["Dependency edge set unchanged"], COMMON_VERIFICATION,
                 ["Dynamic access (getattr, importlib, string registries) is reported, not edited", "Public API and serialization-name warnings require acknowledgement"],
                 category="rename", tags=["multi-file"]),
    RefactorType("python.rename_local", "Rename a local variable or parameter", "applicable", "transform", PY,
                 [_in("rel_path", "string", "File"), _in("function", "string", "Qualified function name"), _in("name", "string", "Current name"), _in("new_name", "string", "New name")],
                 ["Function exists and the name is bound in its own scope"], "One function body", "Binding-accurate scope analysis; nested scopes that rebind the name are skipped",
                 ["identifier replacement"], COMMON_VALIDATION, COMMON_VERIFICATION,
                 ["Keyword-argument callers of renamed parameters are reported as a blocker"], category="rename"),
    RefactorType("python.rename_method", "Rename a class method", "proposal_only", "transform", PY,
                 [_in("rel_path", "string", "File"), _in("class_name", "string", "Class"), _in("name", "string", "Method"), _in("new_name", "string", "New name")],
                 ["Class and method are indexed"], "Class and attribute call sites", "Needs type inference to prove which `obj.method` calls target this class; not available",
                 ["candidate identifier replacements (not applied)"], ["Candidates listed with confidence"], [],
                 ["Receiver types are unknown, so edits are proposed for review but never applied"], reason="Attribute receivers cannot be resolved without type inference.", category="rename"),
    RefactorType("js.rename_symbol", "Rename a top-level JavaScript/TypeScript declaration", "applicable", "transform", JS,
                 [_in("rel_path", "string", "File defining the symbol"), _in("name", "string", "Current name"), _in("new_name", "string", "New name")],
                 ["File is indexed and parses without errors", "Name is bound exactly once in each affected file"], "Declaring file plus ES-module importers resolved by the index",
                 "Conservative: any other binding of the same spelling (parameter, local, pattern) is a blocker instead of being guessed",
                 ["identifier replacement", "import/export specifier replacement", "namespace member replacement"], COMMON_VALIDATION, COMMON_VERIFICATION,
                 ["No scope resolution; CommonJS require users, default exports and shorthand properties are blockers", "tsconfig path aliases are not followed"], category="rename"),
    RefactorType("python.extract_function", "Extract statements into a function", "applicable", "transform", PY,
                 [_in("rel_path", "string", "File"), _in("start_line", "integer", "First line"), _in("end_line", "integer", "Last line"), _in("new_name", "string", "Name of the new function")],
                 ["Selection is whole consecutive statements inside a function or method"], "One enclosing function/method and its container",
                 "Data-flow analysis computes parameters and results; any unsafe construct yields blockers and a proposal-only result",
                 ["region replacement by a call", "function insertion"], COMMON_VALIDATION, COMMON_VERIFICATION,
                 ["return/yield/await/global/nonlocal/del, break/continue crossing the region and closure rebinding are blockers", "No type annotations are generated"], category="extract"),
    RefactorType("analysis.duplicates", "Find duplicated code", "proposal_only", "analysis", PY + JS,
                 [_in("languages", "array", "Language filter", False), _in("min_lines", "integer", "Minimum unit size", False), _in("threshold", "number", "Structural similarity threshold", False),
                  _in("path_prefix", "string", "Restrict to a path", False), _in("exclude_paths", "array", "Extra excluded paths", False)],
                 ["Repository has indexed Python or JS/TS files"], "Functions, methods and statement windows", "Deterministic token/AST fingerprints; similarity is not equivalence",
                 [], ["Analysis only"], [], ["Fragments smaller than a function may be missed", "No automatic merge"], category="analysis"),
    RefactorType("python.simplify_conditionals", "Simplify conditionals", "proposal_only", "analysis", PY,
                 [_in("rel_path", "string", "File")], ["File parses"], "One file", "Each suggestion states its equivalence argument",
                 [], ["Analysis only"], [], ["Suggestions are never applied automatically"], category="simplify"),
    RefactorType("architecture.cycle_suggestions", "Suggest ways to break import cycles", "proposal_only", "analysis", PY + JS,
                 [_in("path_prefix", "string", "Restrict to a path", False)], ["A published index exists"], "File import graph", "Graph analysis of resolved imports",
                 [], ["Analysis only"], [], ["Heuristic suggestions; no edit is generated"], category="architecture"),
]
UNSUPPORTED = [
    ("python.rename_module", "Rename or move a module", "Requires updating packaging, relative imports and string references that cannot be proven complete."),
    ("python.move_symbol", "Move a symbol to another module", "Requires import rewrites in unknown dynamic users and circular-import analysis."),
    ("python.split_function", "Split a function by responsibility", "Requires semantic judgement; use extract-function on selected regions instead."),
    ("python.change_signature", "Change a function signature", "Call-site rewriting needs type inference."),
    ("python.inline_function", "Inline a function", "Requires proving no other callers and evaluation-order equivalence."),
    ("js.extract_function", "Extract a function (JS/TS)", "No data-flow analysis for JavaScript/TypeScript is implemented."),
    ("js.rename_local", "Rename a JS/TS local variable", "No JavaScript/TypeScript scope resolution is implemented."),
]
for _id, _title, _why in UNSUPPORTED:
    TYPES.append(RefactorType(_id, _title, "unsupported", "transform", PY if _id.startswith("python") else JS, [], [], "", "", [], [], [], [], reason=_why, category="unsupported"))

BY_ID = {t.id: t for t in TYPES}


def get(type_id: str) -> RefactorType | None:
    return BY_ID.get(type_id)


def catalog() -> list[dict[str, Any]]:
    order = {"applicable": 0, "proposal_only": 1, "unsupported": 2}
    return [t.as_dict() for t in sorted(TYPES, key=lambda t: (order[t.support], t.id))]
