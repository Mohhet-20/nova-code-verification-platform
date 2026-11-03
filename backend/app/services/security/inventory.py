"""Dependency inventory from manifests and lockfiles. Files are only read: nothing is installed, imported or executed.

Supported: Python (requirements*.txt/.in, pyproject.toml [PEP 621 + Poetry], poetry.lock, uv.lock, Pipfile.lock) and
JavaScript (package.json, package-lock.json / npm-shrinkwrap.json v1-v3). Known manifests of other ecosystems are *reported as unsupported*.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement

PRUNE_DIRS = {".git", "node_modules", "venv", ".venv", "env", "__pycache__", "dist", "build", ".tox", "site-packages", ".mypy_cache", ".pytest_cache", ".ruff_cache",
              ".idea", ".vscode", "vendor", ".eggs", "htmlcov", "coverage"}
UNSUPPORTED = {
    "yarn.lock": "Yarn lockfile", "pnpm-lock.yaml": "pnpm lockfile", "bun.lockb": "Bun lockfile", "Pipfile": "Pipfile (use Pipfile.lock)", "setup.py": "setup.py (not executed)",
    "setup.cfg": "setup.cfg", "environment.yml": "conda environment", "go.mod": "Go modules", "Cargo.lock": "Cargo lockfile", "Cargo.toml": "Cargo manifest",
    "Gemfile.lock": "Bundler lockfile", "pom.xml": "Maven manifest", "build.gradle": "Gradle build", "composer.lock": "Composer lockfile", "composer.json": "Composer manifest",
}
REQ_FILE = re.compile(r"^(?:.*[-_.])?requirements(?:[-_.][\w.-]+)?\.(?:txt|in)$", re.IGNORECASE)
SCOPE_RANK = {"runtime": 0, "optional": 1, "dev": 2, "unknown": 3}


def norm_name(ecosystem: str, name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower() if ecosystem == "PyPI" else name


@dataclass
class FileRec:
    path: str
    kind: str
    sha256: str
    size: int
    status: str  # parsed | malformed | unsupported | too_large | skipped
    detail: str = ""
    components: int = 0


@dataclass
class Comp:
    ecosystem: str
    name: str
    version: str | None = None
    version_state: str = "pinned"
    relationship: str = "direct"
    scope: str = "runtime"
    manifests: list[dict[str, str]] = field(default_factory=list)
    declared: list[str] = field(default_factory=list)
    paths: list[list[str]] = field(default_factory=list)
    license: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.ecosystem, self.name, self.version or "")


@dataclass
class Inventory:
    components: list[Comp]
    files: list[FileRec]
    problems: list[str]
    digest: str
    truncated: bool = False

    def coverage(self) -> dict[str, Any]:
        by: dict[str, int] = {}
        for f in self.files:
            by[f.status] = by.get(f.status, 0) + 1
        return {"files": [vars(f) for f in self.files], "status_counts": by, "problems": self.problems, "truncated": self.truncated,
                "unsupported": [{"path": f.path, "detail": f.detail} for f in self.files if f.status == "unsupported"]}


# ------------------------------------------------------------------------------------------------ discovery
def discover(root: Path, max_files: int) -> tuple[list[tuple[str, Path]], bool, list[str]]:
    """(rel_path, abs_path) for candidate manifests. Symlinks are never followed; every path must stay inside ``root``."""
    out: list[tuple[str, Path]] = []
    skipped: list[str] = []
    seen = 0
    root_real = os.path.realpath(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in PRUNE_DIRS and not os.path.islink(os.path.join(dirpath, d)))
        for fn in sorted(filenames):
            seen += 1
            if seen > max_files * 4:
                return out, True, skipped
            if not (fn in UNSUPPORTED or fn in ("pyproject.toml", "poetry.lock", "uv.lock", "Pipfile.lock", "package.json", "package-lock.json", "npm-shrinkwrap.json") or REQ_FILE.match(fn)):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if os.path.islink(full) or not os.path.realpath(full).startswith(root_real + os.sep):
                skipped.append(rel)
                continue
            out.append((rel, Path(full)))
            if len(out) >= max_files:
                return out, True, skipped
    return out, False, skipped


def _kind(rel: str) -> str:
    fn = rel.rsplit("/", 1)[-1]
    if fn in UNSUPPORTED:
        return "unsupported"
    if REQ_FILE.match(fn):
        return "requirements"
    return fn


# ------------------------------------------------------------------------------------------------ helpers
class _Acc:
    """Accumulates declarations and merges them into components."""

    def __init__(self) -> None:
        self.comps: dict[tuple[str, str, str], Comp] = {}
        self.problems: list[str] = []

    def add(self, c: Comp) -> Comp:
        ex = self.comps.get(c.key)
        if ex is None:
            self.comps[c.key] = c
            return c
        for m in c.manifests:
            if m not in ex.manifests:
                ex.manifests.append(m)
        for d in c.declared:
            if d not in ex.declared:
                ex.declared.append(d)
        for n in c.notes:
            if n not in ex.notes:
                ex.notes.append(n)
        for pth in c.paths:
            if pth not in ex.paths and len(ex.paths) < 3:
                ex.paths.append(pth)
        if c.relationship == "direct" or (ex.relationship == "unknown" and c.relationship != "unknown"):
            ex.relationship = c.relationship if c.relationship != "unknown" else ex.relationship
        if SCOPE_RANK[c.scope] < SCOPE_RANK[ex.scope]:
            ex.scope = c.scope
        ex.license = ex.license or c.license
        return ex


def _scope_for_file(rel: str) -> str:
    low = rel.lower()
    return "dev" if re.search(r"(^|[/_.-])(dev|test|tests|lint|docs|ci|typing)([/_.-]|$)", low) else "runtime"


def _man(rel: str, sha: str) -> dict[str, str]:
    return {"path": rel, "sha256": sha}


def _read(p: Path, limit: int) -> bytes | None:
    if p.stat().st_size > limit:
        return None
    with p.open("rb") as fh:
        return fh.read(limit + 1)


# ------------------------------------------------------------------------------------------------ Python
def _req_component(req: Requirement, rel: str, sha: str, scope: str, acc: _Acc, extra_note: str | None = None) -> Comp | None:
    name = norm_name("PyPI", req.name)
    spec = str(req.specifier)
    pins = [s for s in req.specifier if s.operator in ("==", "===") and not s.version.endswith(".*")]
    notes: list[str] = []
    if req.marker is not None:
        notes.append(f"environment marker not evaluated: {req.marker}")
    if req.url:
        return acc.add(Comp("PyPI", name, None, "unpinned", "direct", scope, [_man(rel, sha)], [f"{rel}: {req.name} @ <url>"], [], None,
                            ["direct URL/VCS reference: version unknown, not assessable against advisories", *notes]))
    if len(pins) == 1 and len(list(req.specifier)) == 1:
        version, state = pins[0].version, "pinned"
    else:
        version, state = None, ("range" if spec else "unpinned")
        notes.append("no exact version pinned: " + (spec or "any version") + " (resolve with a lockfile for an assessable inventory)")
    if extra_note:
        notes.append(extra_note)
    return acc.add(Comp("PyPI", name, version, state, "direct", scope, [_man(rel, sha)], [f"{rel}: {req.name}{spec}"], [], None, notes))


def parse_requirements(root: Path, rel: str, data: bytes, sha: str, acc: _Acc, depth: int = 0, seen: set[str] | None = None) -> tuple[int, list[str]]:
    seen = seen if seen is not None else set()
    seen.add(rel)
    problems: list[str] = []
    text = data.decode("utf-8", errors="replace")
    lines: list[str] = []
    buf = ""
    for raw in text.splitlines():
        line = re.sub(r"(^|\s)#.*$", "", raw).rstrip()
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        lines.append((buf + line).strip())
        buf = ""
    count = 0
    scope = _scope_for_file(rel)
    seen_names: dict[str, str | None] = {}
    for line in lines:
        if not line:
            continue
        inc_m = re.match(r"^(?:-r\s*|--requirement[\s=]+)(\S.*)$", line)
        if inc_m:
            target = inc_m.group(1).strip()
            if depth >= 5:
                problems.append(f"{rel}: include depth limit reached at '{target[:80]}'")
                continue
            inc = os.path.normpath(os.path.join(os.path.dirname(rel), target)).replace(os.sep, "/")
            full = (root / inc)
            if inc.startswith("..") or os.path.isabs(target) or not full.is_file() or full.is_symlink() or not os.path.realpath(full).startswith(os.path.realpath(root) + os.sep):
                problems.append(f"{rel}: include '{target[:80]}' is outside the repository or missing")
                continue
            if inc in seen:
                problems.append(f"{rel}: include cycle at '{inc}'")
                continue
            blob = _read(full, 2_000_000)
            if blob is None:
                problems.append(f"{rel}: include '{inc}' is too large")
                continue
            n, p = parse_requirements(root, inc, blob, hashlib.sha256(blob).hexdigest(), acc, depth + 1, seen)
            count += n
            problems.extend(p)
            continue
        if line.startswith(("-c ", "--constraint")):
            problems.append(f"{rel}: constraint file '{line[:80]}' is not applied to the inventory")
            continue
        if line.startswith(("-e ", "--editable")):
            problems.append(f"{rel}: editable/local install '{line[:80]}' is not assessable")
            continue
        if line.startswith("-"):
            continue  # index/option lines
        if re.match(r"^(git\+|hg\+|svn\+|bzr\+|https?://|file:)", line):
            egg = re.search(r"[#&]egg=([A-Za-z0-9_.-]+)", line)
            if egg:
                acc.add(Comp("PyPI", norm_name("PyPI", egg.group(1)), None, "unpinned", "direct", scope, [_man(rel, sha)], [f"{rel}: {egg.group(1)} @ <url>"], [], None,
                             ["direct URL/VCS reference: version unknown, not assessable against advisories"]))
                count += 1
            else:
                problems.append(f"{rel}: a direct URL/VCS requirement without a '#egg=' package name cannot be identified")
            continue
        try:
            req = Requirement(line)
        except InvalidRequirement:
            problems.append(f"{rel}: could not parse requirement '{line[:80]}'")
            continue
        c = _req_component(req, rel, sha, scope, acc)
        count += 1
        if c is not None:
            if c.name in seen_names:
                problems.append(f"{rel}: duplicate declaration of {c.name}" + ("" if seen_names[c.name] == c.version else f" with a different version ({seen_names[c.name]} vs {c.version})"))
            seen_names[c.name] = c.version
    return count, problems


def _pep621_deps(data: dict[str, Any], rel: str, sha: str, acc: _Acc) -> tuple[list[tuple[str, str]], int]:
    """Returns (root dependency names with scope, number of declarations)."""
    roots: list[tuple[str, str]] = []
    n = 0
    proj = data.get("project") or {}
    groups: list[tuple[str, list[Any]]] = [("runtime", proj.get("dependencies") or [])]
    for gname, lst in (proj.get("optional-dependencies") or {}).items():
        groups.append(("optional" if not re.search(r"dev|test|lint|doc", gname) else "dev", lst))
    for lst in (data.get("dependency-groups") or {}).values():
        groups.append(("dev", [x for x in lst if isinstance(x, str)]))
    groups.append(("dev", (data.get("build-system") or {}).get("requires") or []))
    for scope, lst in groups:
        for item in lst if isinstance(lst, list) else []:
            if not isinstance(item, str):
                continue
            try:
                req = Requirement(item)
            except InvalidRequirement:
                acc.problems.append(f"{rel}: could not parse requirement '{item[:80]}'")
                continue
            c = _req_component(req, rel, sha, scope, acc)
            if c is not None:
                roots.append((c.name, scope))
                n += 1
    poetry = (data.get("tool") or {}).get("poetry") or {}
    pgroups: list[tuple[str, dict[str, Any]]] = [("runtime", poetry.get("dependencies") or {}), ("dev", poetry.get("dev-dependencies") or {})]
    for gname, g in (poetry.get("group") or {}).items():
        pgroups.append(("dev" if re.search(r"dev|test|lint|doc", gname) else "optional", (g or {}).get("dependencies") or {}))
    for scope, deps in pgroups:
        for dname, spec in deps.items():
            if dname.lower() == "python":
                continue
            version_spec = spec if isinstance(spec, str) else (spec.get("version") if isinstance(spec, dict) else None)
            try:
                if isinstance(spec, dict) and any(k in spec for k in ("git", "path", "url")):
                    req = Requirement(f"{dname} @ https://example.invalid/placeholder")
                else:
                    req = Requirement(f"{dname}{_poetry_spec(version_spec)}")
            except (InvalidRequirement, ValueError):
                acc.problems.append(f"{rel}: could not parse Poetry dependency '{dname}'")
                continue
            c = _req_component(req, rel, sha, scope, acc)
            if c is not None:
                roots.append((c.name, scope))
                n += 1
    return roots, n


def _poetry_spec(spec: str | None) -> str:
    if not spec or spec == "*":
        return ""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if part.startswith("^"):
            base = part[1:]
            nums = [int(x) if x.isdigit() else 0 for x in re.split(r"[.]", base)[:3]]
            nums += [0] * (3 - len(nums))
            if nums[0] > 0:
                hi = f"{nums[0] + 1}.0.0"
            elif nums[1] > 0:
                hi = f"0.{nums[1] + 1}.0"
            else:
                hi = f"0.0.{nums[2] + 1}"
            out.append(f">={base},<{hi}")
        elif part.startswith("~") and not part.startswith("~="):
            base = part[1:]
            nums = [int(x) if x.isdigit() else 0 for x in base.split(".")[:3]]
            hi = f"{nums[0]}.{nums[1] + 1}.0" if len(nums) > 1 else f"{nums[0] + 1}.0.0"
            out.append(f">={base},<{hi}")
        elif part and part[0].isdigit():
            out.append(f"=={part}")
        else:
            out.append(part)
    return ",".join(out)


def _bfs_paths(graph: dict[str, list[str]], roots: list[str], limit_paths: int = 3, max_depth: int = 12) -> dict[str, list[list[str]]]:
    paths: dict[str, list[list[str]]] = {}
    queue: deque[list[str]] = deque([[r] for r in roots if r in graph or True])
    while queue:
        p = queue.popleft()
        node = p[-1]
        lst = paths.setdefault(node, [])
        if p in lst or len(lst) >= limit_paths:
            continue
        lst.append(p)
        if len(p) >= max_depth:
            continue
        for nxt in sorted(graph.get(node, [])):
            if nxt not in p:
                queue.append([*p, nxt])
    return paths


def _lock_graph_components(acc: _Acc, eco: str, rel: str, sha: str, packages: dict[str, dict[str, Any]], graph: dict[str, list[str]], roots: list[tuple[str, str]],
                           root_note: str | None = None) -> int:
    """packages: node-id -> {name, version, license?}; graph: node-id -> [node-id]; roots: [(node-id, scope)]."""
    reach: dict[str, str] = {}
    best_paths: dict[str, list[list[str]]] = {}
    for scope in ("runtime", "optional", "dev"):
        rs = [r for r, s in roots if s == scope]
        for node, ps in _bfs_paths(graph, rs).items():
            if node not in reach:
                reach[node] = scope
                best_paths[node] = ps
    direct = {r for r, _ in roots}
    n = 0
    for node, info in packages.items():
        pretty = lambda x: packages.get(x, {}).get("name", x)  # noqa: E731
        paths = [[f"{pretty(x)}" for x in p] for p in best_paths.get(node, [])]
        notes = [root_note] if root_note and node not in reach else []
        c = Comp(eco, info["name"], info.get("version"), "pinned" if info.get("version") else "unpinned", "direct" if node in direct else ("transitive" if node in reach else "unknown"),
                 reach.get(node, info.get("scope", "unknown")), [_man(rel, sha)], [], paths, info.get("license"), notes)
        acc.add(c)
        n += 1
    return n


def parse_poetry_lock(data: bytes, rel: str, sha: str, acc: _Acc, roots: list[tuple[str, str]]) -> int:
    doc = tomllib.loads(data.decode("utf-8"))
    pkgs: dict[str, dict[str, Any]] = {}
    graph: dict[str, list[str]] = {}
    for p in doc.get("package", []):
        nm = norm_name("PyPI", p["name"])
        pkgs[nm] = {"name": nm, "version": p.get("version")}
        graph[nm] = [norm_name("PyPI", d) for d in (p.get("dependencies") or {})]
    missing = [r for r, _ in roots if r not in pkgs]
    for m in missing:
        acc.problems.append(f"{rel}: '{m}' is declared in pyproject.toml but absent from the lockfile (stale lockfile?)")
    return _lock_graph_components(acc, "PyPI", rel, sha, pkgs, graph, [(r, s) for r, s in roots if r in pkgs], "not reachable from a declared dependency")


def parse_uv_lock(data: bytes, rel: str, sha: str, acc: _Acc, roots: list[tuple[str, str]]) -> int:
    doc = tomllib.loads(data.decode("utf-8"))
    pkgs: dict[str, dict[str, Any]] = {}
    graph: dict[str, list[str]] = {}
    root_nodes: list[tuple[str, str]] = []
    for p in doc.get("package", []):
        nm = norm_name("PyPI", p["name"])
        src = p.get("source") or {}
        deps = [norm_name("PyPI", d["name"]) for d in (p.get("dependencies") or []) if isinstance(d, dict) and "name" in d]
        if "virtual" in src or "editable" in src:
            root_nodes.extend((d, "runtime") for d in deps)
            for _g, lst in (p.get("dev-dependencies") or {}).items():
                root_nodes.extend((norm_name("PyPI", d["name"]), "dev") for d in lst if isinstance(d, dict))
            for _g, lst in (p.get("optional-dependencies") or {}).items():
                root_nodes.extend((norm_name("PyPI", d["name"]), "optional") for d in lst if isinstance(d, dict))
            continue
        pkgs[nm] = {"name": nm, "version": p.get("version")}
        graph[nm] = deps
    use = root_nodes or roots
    return _lock_graph_components(acc, "PyPI", rel, sha, pkgs, graph, [(r, s) for r, s in use if r in pkgs])


def parse_pipfile_lock(data: bytes, rel: str, sha: str, acc: _Acc) -> int:
    doc = json.loads(data.decode("utf-8"))
    n = 0
    for section, scope in (("default", "runtime"), ("develop", "dev")):
        for name, info in (doc.get(section) or {}).items():
            v = (info.get("version") or "") if isinstance(info, dict) else ""
            version = v[2:] if v.startswith("==") else None
            nm = norm_name("PyPI", name)
            acc.add(Comp("PyPI", nm, version, "pinned" if version else "unpinned", "unknown", scope, [_man(rel, sha)], [], [], None,
                         ["Pipfile.lock does not record which packages are direct"]))
            n += 1
    return n


# ------------------------------------------------------------------------------------------------ npm
def _pkg_json(doc: dict[str, Any], rel: str, sha: str, acc: _Acc) -> tuple[list[tuple[str, str]], int]:
    roots: list[tuple[str, str]] = []
    n = 0
    for key, scope in (("dependencies", "runtime"), ("devDependencies", "dev"), ("optionalDependencies", "optional")):
        deps = doc.get(key) or {}
        if not isinstance(deps, dict):
            acc.problems.append(f"{rel}: '{key}' is not an object")
            continue
        for name, spec in sorted(deps.items()):
            spec = str(spec)
            exact = re.fullmatch(r"v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)", spec.strip())
            notes = [] if exact else [f"declared as '{spec[:60]}' (not an exact version)"]
            if re.match(r"^(git|http|https|file|link|github:|workspace:)", spec) or "/" in spec and not spec.startswith("@"):
                notes = [f"non-registry source '{spec[:60]}': not assessable against advisories"]
                exact = None
            acc.add(Comp("npm", name, exact.group(1) if exact else None, "pinned" if exact else "range", "direct", scope, [_man(rel, sha)], [f"{rel}: {name}@{spec}"], [], None, notes))
            roots.append((name, scope))
            n += 1
    if doc.get("peerDependencies"):
        acc.problems.append(f"{rel}: peerDependencies are not part of the installed inventory")
    return roots, n


def parse_package_lock(data: bytes, rel: str, sha: str, acc: _Acc, roots: list[tuple[str, str]]) -> int:
    doc = json.loads(data.decode("utf-8"))
    lv = doc.get("lockfileVersion", 1)
    pk: dict[str, dict[str, Any]] = {}
    if isinstance(doc.get("packages"), dict):
        pk = {k: v for k, v in doc["packages"].items() if isinstance(v, dict)}
    elif isinstance(doc.get("dependencies"), dict):  # lockfile v1: nested tree
        def walk(prefix: str, deps: dict[str, Any]) -> None:
            for name, info in deps.items():
                if not isinstance(info, dict):
                    continue
                path = f"{prefix}node_modules/{name}"
                pk[path] = {"version": info.get("version"), "dev": info.get("dev"), "optional": info.get("optional"),
                            "dependencies": dict.fromkeys(info.get("requires") or {}, "*")}
                if isinstance(info.get("dependencies"), dict):
                    walk(path + "/", info["dependencies"])
        walk("", doc["dependencies"])
        pk[""] = {"dependencies": {n: "*" for n, _ in roots}}
    else:
        acc.problems.append(f"{rel}: lockfileVersion {lv} has no package data")
        return 0
    root_entry = pk.get("", {})
    nodes: dict[str, dict[str, Any]] = {}
    for path, info in pk.items():
        if path == "" or "node_modules/" not in path:
            continue
        if info.get("link"):
            continue
        name = info.get("name") or path.rsplit("node_modules/", 1)[-1]
        nodes[path] = {"name": name, "version": info.get("version"), "license": info.get("license") if isinstance(info.get("license"), str) else None,
                       "scope": "dev" if info.get("dev") else ("optional" if info.get("optional") else "runtime")}

    def resolve(frm: str, dep: str) -> str | None:
        base = frm
        while True:
            cand = (base + "/" if base else "") + "node_modules/" + dep
            if cand in nodes:
                return cand
            if not base:
                return None
            idx = base.rfind("/node_modules/")
            base = base[:idx] if idx >= 0 else ""

    graph: dict[str, list[str]] = {}
    for path, info in pk.items():
        if path not in nodes:
            continue
        deps = {**(info.get("dependencies") or {}), **(info.get("optionalDependencies") or {})}
        graph[path] = [t for t in (resolve(path, d) for d in sorted(deps)) if t]
    rs: list[tuple[str, str]] = []
    for key, scope in (("dependencies", "runtime"), ("devDependencies", "dev"), ("optionalDependencies", "optional")):
        for d in sorted(root_entry.get(key) or {}):
            t = resolve("", d)
            if t:
                rs.append((t, scope))
    if not rs:
        rs = [(t, s) for n, s in roots if (t := resolve("", n))]
    for n_, _ in roots:
        if resolve("", n_) is None:
            acc.problems.append(f"{rel}: '{n_}' is declared in package.json but absent from the lockfile (stale lockfile?)")
    return _lock_graph_components(acc, "npm", rel, sha, nodes, graph, rs)


# ------------------------------------------------------------------------------------------------ driver
def build(root: Path, max_files: int, max_bytes: int, max_components: int) -> Inventory:
    acc = _Acc()
    files: list[FileRec] = []
    cands, truncated, skipped = discover(root, max_files)
    for s in skipped:
        files.append(FileRec(s, _kind(s), "", 0, "skipped", "symbolic link or path outside the repository was not followed"))
    py_roots: dict[str, list[tuple[str, str]]] = {}
    js_roots: dict[str, list[tuple[str, str]]] = {}
    ordered = sorted(cands, key=lambda t: (0 if t[0].endswith(("pyproject.toml", "package.json")) else 1, t[0]))

    def record(rel: str, kind: str, data: bytes | None, size: int, status: str, detail: str, count: int = 0) -> None:
        files.append(FileRec(rel, kind, hashlib.sha256(data).hexdigest() if data is not None else "", size, status, detail, count))

    for rel, path in ordered:
        kind = _kind(rel)
        try:
            size = path.stat().st_size
        except OSError:
            record(rel, kind, None, 0, "skipped", "unreadable")
            continue
        if kind == "unsupported":
            record(rel, kind, None, size, "unsupported", UNSUPPORTED.get(rel.rsplit("/", 1)[-1], "unsupported") + ": not parsed; its dependencies are NOT in the inventory")
            continue
        data = _read(path, max_bytes)
        if data is None:
            record(rel, kind, None, size, "too_large", f"larger than {max_bytes} bytes")
            continue
        sha = hashlib.sha256(data).hexdigest()
        d = os.path.dirname(rel)
        try:
            if kind == "requirements":
                n, probs = parse_requirements(root, rel, data, sha, acc)
                acc.problems.extend(probs)
            elif kind == "pyproject.toml":
                doc = tomllib.loads(data.decode("utf-8"))
                roots, n = _pep621_deps(doc, rel, sha, acc)
                py_roots[d] = roots
            elif kind == "poetry.lock":
                n = parse_poetry_lock(data, rel, sha, acc, py_roots.get(d, []))
            elif kind == "uv.lock":
                n = parse_uv_lock(data, rel, sha, acc, py_roots.get(d, []))
            elif kind == "Pipfile.lock":
                n = parse_pipfile_lock(data, rel, sha, acc)
            elif kind == "package.json":
                doc = json.loads(data.decode("utf-8"))
                if not isinstance(doc, dict):
                    raise ValueError("top level is not an object")
                roots, n = _pkg_json(doc, rel, sha, acc)
                js_roots[d] = roots
            elif kind in ("package-lock.json", "npm-shrinkwrap.json"):
                n = parse_package_lock(data, rel, sha, acc, js_roots.get(d, []))
            else:
                n = 0
            record(rel, kind, data, size, "parsed", "", n)
        except (ValueError, KeyError, TypeError, tomllib.TOMLDecodeError, UnicodeDecodeError, RecursionError, AttributeError) as exc:
            record(rel, kind, data, size, "malformed", f"{type(exc).__name__}: could not be parsed")
    # lockfile-resolved components supersede the manifest-only declaration of the same package
    comps = list(acc.comps.values())
    resolved_names = {(c.ecosystem, c.name) for c in comps if c.version and any(m["path"].endswith(("lock", "lock.json", "shrinkwrap.json")) for m in c.manifests)}
    merged: dict[tuple[str, str, str], Comp] = {}
    for c in comps:
        if not c.version and (c.ecosystem, c.name) in resolved_names:
            for lc in comps:
                if lc.ecosystem == c.ecosystem and lc.name == c.name and lc.version:
                    for d in c.declared:
                        if d not in lc.declared:
                            lc.declared.append(d)
                    for m in c.manifests:
                        if m not in lc.manifests:
                            lc.manifests.append(m)
                    lc.relationship = "direct" if c.relationship == "direct" else lc.relationship
            continue
        merged[c.key] = c
    final = sorted(merged.values(), key=lambda c: (c.ecosystem, c.name, c.version or ""))
    # conflicting pins across manifests
    by_name: dict[tuple[str, str], set[str]] = {}
    for c in final:
        if c.version and c.relationship == "direct":
            by_name.setdefault((c.ecosystem, c.name), set()).add(c.version)
    for (eco, name), vs in sorted(by_name.items()):
        if len(vs) > 1:
            acc.problems.append(f"conflicting versions of {eco} package {name}: {', '.join(sorted(vs))}")
    if len(final) > max_components:
        final, truncated = final[:max_components], True
        acc.problems.append(f"component limit {max_components} reached; the inventory is incomplete")
    for c in final:
        c.manifests.sort(key=lambda m: m["path"])
        c.declared.sort()
        c.notes = sorted(set(c.notes))
    problems = sorted(set(acc.problems))
    files.sort(key=lambda f: f.path)
    canon = json.dumps({"components": [[c.ecosystem, c.name, c.version, c.version_state, c.relationship, c.scope, c.manifests, c.declared] for c in final],
                        "files": [[f.path, f.sha256, f.status] for f in files]}, sort_keys=True, separators=(",", ":"))
    return Inventory(final, files, problems, hashlib.sha256(canon.encode()).hexdigest(), truncated)
