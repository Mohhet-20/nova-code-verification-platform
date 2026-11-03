"""Deterministic synthetic repositories. Same (profile, seed, version) -> byte-identical tree.

Profile names are labels for generator parameters, NOT claims about any real-world repository size: always read the manifest
(actual file counts, bytes, lines, language mix, dependency counts and import edges).
"""
from __future__ import annotations

import hashlib
import random
from pathlib import Path
from typing import Any

FIXTURE_VERSION = "1"

# python_modules, ts_modules, doc_files, nesting depth, imports per module, dependencies, extra: special content
PROFILES: dict[str, dict[str, Any]] = {
    "small": {"py": 12, "ts": 6, "docs": 4, "depth": 2, "imports": 3, "deps": 6},
    "medium": {"py": 150, "ts": 60, "docs": 40, "depth": 4, "imports": 5, "deps": 25},
    "large": {"py": 900, "ts": 350, "docs": 250, "depth": 6, "imports": 6, "deps": 80},
    "many_files_little_code": {"py": 10, "ts": 5, "docs": 1500, "depth": 3, "imports": 2, "deps": 4},
    "large_source_files": {"py": 6, "ts": 2, "docs": 2, "depth": 1, "imports": 3, "deps": 4, "funcs_per_module": 2500},
    "deep_nesting": {"py": 60, "ts": 10, "docs": 5, "depth": 11, "imports": 3, "deps": 6},
    "import_heavy": {"py": 200, "ts": 40, "docs": 5, "depth": 3, "imports": 20, "deps": 10},
    "malformed": {"py": 60, "ts": 20, "docs": 5, "depth": 3, "imports": 3, "deps": 4, "malformed_every": 3},
    "many_dependencies": {"py": 20, "ts": 10, "docs": 3, "depth": 2, "imports": 3, "deps": 600},
}


def _py_module(rng: random.Random, name: str, siblings: list[str], imports: int, funcs: int, broken: bool) -> str:
    lines = [f'"""Synthetic module {name}."""', "import os", "import json"]
    for target in rng.sample(siblings, min(imports, len(siblings))) if siblings else []:
        lines.append(f"from {target} import fn_0 as dep_{target.split('.')[-1]}")
    lines.append("")
    for i in range(funcs):
        lines += [f"def fn_{i}(a, b=None):", f'    """Function {i} of {name}."""', "    total = 0", "    for k in range(a or 1):", "        total += k * 2",
                  "    if b is not None:", "        total += len(str(b))", f"    return total + {rng.randint(0, 99)}", ""]
    lines += [f"class Cls_{name.split('.')[-1]}:", "    def method(self, x):", f"        return fn_0(x) + {rng.randint(0, 9)}", ""]
    if broken:
        lines.append("def broken(:")
    return "\n".join(lines) + "\n"


def _ts_module(rng: random.Random, siblings: list[str], imports: int, broken: bool) -> str:
    lines = []
    for target in rng.sample(siblings, min(imports, len(siblings))) if siblings else []:
        lines.append(f'import {{ item0 as d{abs(hash(target)) % 10**6} }} from "{target}";')
    for i in range(4):
        lines += [f"export function item{i}(a: number): number {{", f"  return a * {rng.randint(1, 9)};", "}"]
    lines.append("export class Widget { run(x: number) { return item0(x); } }")
    if broken:
        lines.append("export function oops( {")
    return "\n".join(lines) + "\n"


def generate(root: Path, profile: str = "small", seed: int = 1234) -> dict[str, Any]:
    p = PROFILES[profile]
    rng = random.Random(f"{FIXTURE_VERSION}:{profile}:{seed}")  # noqa: S311 - deterministic fixtures, not security
    root.mkdir(parents=True, exist_ok=True)
    written: dict[str, bytes] = {}

    def put(rel: str, text: str) -> None:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        path.write_bytes(data)
        written[rel] = data

    def dirpath(i: int) -> str:
        depth = 1 + (i % p["depth"])
        return "/".join(f"pkg{(i // (j + 1)) % 5}" for j in range(depth))

    py_names: list[tuple[str, str]] = []  # (module dotted, rel path)
    for i in range(p["py"]):
        d = dirpath(i)
        py_names.append((f"{d.replace('/', '.')}.mod{i}", f"src/{d}/mod{i}.py"))
    for i, (dotted, rel) in enumerate(py_names):
        sibs = [n for n, _ in py_names[:i]]  # earlier modules only: acyclic, deterministic
        broken = bool(p.get("malformed_every")) and i % p["malformed_every"] == 0
        put(rel, _py_module(rng, dotted, ["src." + s for s in sibs], p["imports"], p.get("funcs_per_module", 6), broken))
    # package markers so Python resolution has packages
    for rel in sorted({str(Path(r).parent) for _, r in py_names}):
        parts = Path(rel).parts
        for k in range(1, len(parts) + 1):
            init = "/".join(parts[:k]) + "/__init__.py"
            if init not in written:
                put(init, "")
    ts_paths = []
    for i in range(p["ts"]):
        d = dirpath(i)
        ts_paths.append(f"web/{d}/comp{i}.ts")
    for i, rel in enumerate(ts_paths):
        sibs = ["./" + "/".join(Path(s).parts[Path(s).parts.index("web") + 1:]).rsplit(".", 1)[0] for s in ts_paths[:i]]
        # relative specifiers computed from the importing file's own directory
        base = Path(rel).parent
        rels = []
        for s in ts_paths[:i]:
            r = Path(s).with_suffix("")
            try:
                rel_spec = str(Path(*(".." for _ in base.parts[1:]), *r.parts[1:])) if len(base.parts) > 1 else "./" + "/".join(r.parts[1:])
            except Exception:  # noqa: BLE001
                rel_spec = "./x"
            rels.append(rel_spec if rel_spec.startswith(".") else "./" + rel_spec)
        broken = bool(p.get("malformed_every")) and i % p["malformed_every"] == 0
        put(rel, _ts_module(rng, rels or sibs, p["imports"], broken))
    for i in range(p["docs"]):
        put(f"docs/{dirpath(i)}/note{i}.md", f"# Note {i}\n\n" + "Lorem ipsum synthetic text.\n" * rng.randint(1, 5))
    put("requirements.txt", "".join(f"pkg-synthetic-{i}=={1 + i % 5}.{i % 7}.{i % 3}\n" for i in range(p["deps"])))
    put("package.json", '{"name": "synthetic", "version": "1.0.0", "dependencies": {' + ", ".join(f'"js-synthetic-{i}": "^{1 + i % 4}.{i % 6}.0"' for i in range(min(p["deps"], 200))) + "}}\n")
    put("README.md", f"# Synthetic {profile}\n")
    h = hashlib.sha256()
    for rel in sorted(written):
        h.update(rel.encode() + b"\0" + hashlib.sha256(written[rel]).digest())
    langs = {"python": sum(1 for r in written if r.endswith(".py")), "typescript": sum(1 for r in written if r.endswith(".ts")), "markdown": sum(1 for r in written if r.endswith(".md")), "other": sum(1 for r in written if not r.endswith((".py", ".ts", ".md")))}
    return {"fixture_version": FIXTURE_VERSION, "profile": profile, "seed": seed, "files": len(written), "bytes": sum(len(v) for v in written.values()),
            "lines": sum(v.count(b"\n") for v in written.values()), "languages": langs, "dependencies": p["deps"] + min(p["deps"], 200), "import_statements_target": (p["py"] + p["ts"]) * p["imports"],
            "malformed_files": (-(-p["py"] // p["malformed_every"]) + -(-p["ts"] // p["malformed_every"])) if p.get("malformed_every") else 0, "digest": h.hexdigest()[:16]}


def touch_one(root: Path, manifest: dict[str, Any]) -> str:
    """Deterministic small change for incremental benchmarks: append a function to the lexicographically first python module."""
    target = sorted(root.rglob("mod*.py"))[0]
    with target.open("a", encoding="utf-8") as fh:
        fh.write("\n\ndef added_for_incremental(x):\n    return x + 1\n")
    return str(target.relative_to(root)).replace("\\", "/")
