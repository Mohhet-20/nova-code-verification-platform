"""Import resolution: maps import records to repository files, declared packages, or an honest 'unresolved'.

Pure functions over (indexed path set, declared dependencies); no filesystem access, no execution.
Resolution is syntactic and conventional (module path rules); it is not compiler-accurate.
"""
from __future__ import annotations

import posixpath
import sys
from dataclasses import dataclass, field

from app.services.intelligence.manifests import normalize_py
from app.services.intelligence.types import EXTERNAL, RESOLVED, UNRESOLVED, UNSUPPORTED_RES

PY_LANGS = {"Python"}
JS_LANGS = {"JavaScript", "JSX", "TypeScript", "TSX"}

PY_DIST_ALIASES = {"yaml": "pyyaml", "pil": "pillow", "cv2": "opencv_python", "sklearn": "scikit_learn",
                   "bs4": "beautifulsoup4", "dateutil": "python_dateutil", "jwt": "pyjwt",
                   "dotenv": "python_dotenv", "attr": "attrs", "google": "protobuf"}
JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".mts", ".cts")
JS_SWAP = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}
NODE_BUILTINS = frozenset("""assert async_hooks buffer child_process cluster console constants crypto dgram dns domain
events fs http http2 https inspector module net os path perf_hooks process punycode querystring readline repl
stream string_decoder sys timers tls trace_events tty url util v8 vm wasi worker_threads zlib""".split())
STDLIB = frozenset(getattr(sys, "stdlib_module_names", ()))


@dataclass(frozen=True)
class Resolution:
    status: str                      # resolved | external | unresolved | unsupported
    targets: tuple[str, ...] = ()    # repository-relative paths
    package: str | None = None       # declared/external package name
    note: str = ""


@dataclass
class ResolverContext:
    paths: frozenset[str]
    declared_py: frozenset[str] = frozenset()
    declared_js_by_dir: dict[str, frozenset[str]] = field(default_factory=dict)  # dir ('' = root) -> names

    def js_declared(self, file_path: str) -> frozenset[str] | None:
        d = posixpath.dirname(file_path)
        while True:
            if d in self.declared_js_by_dir:
                return self.declared_js_by_dir[d]
            if not d:
                return None
            d = posixpath.dirname(d)


def _join(*parts: str) -> str:
    return posixpath.normpath("/".join(p for p in parts if p))


def _py_roots(ctx: ResolverContext, file_path: str) -> list[str]:
    roots: list[str] = []
    d = posixpath.dirname(file_path)
    top = d
    while d and f"{d}/__init__.py" in ctx.paths:
        top = posixpath.dirname(d)
        d = top
    if top != posixpath.dirname(file_path) or f"{posixpath.dirname(file_path)}/__init__.py" in ctx.paths:
        roots.append(top)
    for extra in ("", "src", "lib"):
        if extra not in roots:
            roots.append(extra)
    return roots


def _py_module_file(ctx: ResolverContext, base: str) -> str | None:
    for cand in (f"{base}.py", f"{base}/__init__.py", f"{base}.pyi") if base else ("__init__.py",):
        if cand in ctx.paths:
            return cand
    return None


def resolve_python(ctx: ResolverContext, file_path: str, spec: str, level: int, names: list[str]) -> Resolution:
    rel = spec.replace(".", "/") if spec else ""
    bases: list[str] = []
    if level > 0:
        d = posixpath.dirname(file_path)
        for _ in range(level - 1):
            d = posixpath.dirname(d)
        bases.append(_join(d, rel) if rel else d)
    else:
        bases.extend(_join(root, rel) if rel else root for root in _py_roots(ctx, file_path))
    targets: list[str] = []
    module_file = None
    module_base = None
    for base in bases:
        module_file = _py_module_file(ctx, base.strip("."))
        if module_file:
            module_base = base
            break
    if module_file:
        targets.append(module_file)
    # `from pkg import submodule` depends on the submodule file when one exists
    pkg_dirs = [module_base] if module_base is not None else bases
    for n in names:
        if n == "*":
            continue
        for pkg_dir in pkg_dirs:
            sub = _py_module_file(ctx, _join(pkg_dir, n)) if pkg_dir is not None else None
            if sub and sub not in targets and sub != module_file:
                targets.append(sub)
                break
    if targets:
        return Resolution(RESOLVED, tuple(targets))
    if level > 0:
        return Resolution(UNRESOLVED, note="relative import target not found in repository")
    top = spec.split(".", 1)[0]
    if top in STDLIB:
        return Resolution(EXTERNAL, package=top, note="python standard library")
    dist = normalize_py(top)
    if dist in ctx.declared_py or PY_DIST_ALIASES.get(top.lower()) in ctx.declared_py:
        return Resolution(EXTERNAL, package=top, note="declared dependency")
    return Resolution(UNRESOLVED, note="not found in repository files or declared dependencies")


def resolve_javascript(ctx: ResolverContext, file_path: str, spec: str) -> Resolution:
    if not spec:
        return Resolution(UNRESOLVED, note="empty module specifier")
    if spec.startswith(("./", "../")) or spec in (".", ".."):
        base = _join(posixpath.dirname(file_path), spec)
        if base == ".." or base.startswith("../"):
            return Resolution(UNRESOLVED, note="path escapes the repository root")
        ext = posixpath.splitext(base)[1]
        cands = [base] + [base + e for e in JS_EXTS] + [f"{base}/index{e}" for e in JS_EXTS]
        for swap in JS_SWAP.get(ext, ()):
            cands.insert(1, base[: -len(ext)] + swap)
        for c in cands:
            if c in ctx.paths:
                return Resolution(RESOLVED, (c,))
        return Resolution(UNRESOLVED, note="relative import target not found in repository")
    if spec.startswith(("/", "@/", "~/", "#")):
        return Resolution(UNSUPPORTED_RES, note="absolute/alias/subpath-import resolution is not implemented")
    if spec.startswith("node:") or spec.split("/", 1)[0] in NODE_BUILTINS:
        return Resolution(EXTERNAL, package=spec.removeprefix("node:"), note="node builtin")
    parts = spec.split("/")
    pkg = "/".join(parts[:2]) if spec.startswith("@") and len(parts) > 1 else parts[0]
    declared = ctx.js_declared(file_path)
    if declared is not None and pkg in declared:
        return Resolution(EXTERNAL, package=pkg, note="declared dependency")
    return Resolution(UNRESOLVED, package=pkg, note="not declared in the nearest package.json" if declared is not None
                      else "no package.json found for this file")
