"""Extract-function analysis and (when provably safe) generation for Python.

The analysis is deliberately conservative: it either produces a rewrite whose data flow it can fully account for, or it returns
blockers explaining why not (the plan is then proposal-only). It does not infer types, never executes the code, and the generated
file is re-parsed and compared with the original by the undefined-name check before it is offered.
"""
from __future__ import annotations

import ast
import io
import keyword
import tokenize
from collections import defaultdict
from dataclasses import dataclass, field

from app.services.analysis.redaction import scrub
from app.services.refactor import pyscope as ps
from app.services.refactor.common import PlanResult, Workspace, loc

IO_CALLS = {"print", "open", "input", "exec", "eval", "compile", "__import__"}
IO_MODULES = {"os", "sys", "subprocess", "socket", "requests", "urllib", "shutil", "pathlib", "logging", "time", "random", "datetime", "sqlite3", "httpx"}
DYNAMIC = {"locals", "vars", "globals", "eval", "exec", "dir"}


@dataclass
class Acc:
    reads: dict[str, list[tuple[int, int]]] = field(default_factory=lambda: defaultdict(list))
    writes: dict[str, list[tuple[int, int]]] = field(default_factory=lambda: defaultdict(list))
    deleted: set[str] = field(default_factory=set)
    decls: set[str] = field(default_factory=set)
    closures: list[tuple[int, set[str]]] = field(default_factory=list)
    raises: list[int] = field(default_factory=list)
    effects: list[tuple[int, str]] = field(default_factory=list)
    unsupported: list[tuple[int, str]] = field(default_factory=list)


def _args_names(a: ast.arguments) -> list[str]:
    names = [x.arg for x in [*a.posonlyargs, *a.args, *a.kwonlyargs]]
    if a.vararg:
        names.append(a.vararg.arg)
    if a.kwarg:
        names.append(a.kwarg.arg)
    return names


def _targets(t: ast.AST) -> list[str]:
    if isinstance(t, ast.Name):
        return [t.id]
    if isinstance(t, (ast.Tuple, ast.List)):
        return [n for e in t.elts for n in _targets(e)]
    if isinstance(t, ast.Starred):
        return _targets(t.value)
    return []


def visit(node: ast.AST, acc: Acc) -> None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        if not isinstance(node, ast.Lambda):
            acc.writes[node.name].append((node.lineno, node.col_offset))
            for d in node.decorator_list:
                visit(d, acc)
        for d in [*node.args.defaults, *[k for k in node.args.kw_defaults if k is not None]]:
            visit(d, acc)
        inner = Acc()
        body = node.body if isinstance(node.body, list) else [node.body]
        for b in body:
            visit(b, inner)
        params = set(_args_names(node.args))
        free = {n for n in inner.reads if n not in params and n not in inner.writes}
        free |= {n for n in inner.decls}
        for n in free:
            acc.reads[n].append((node.lineno, node.col_offset))
        acc.closures.append((node.lineno, free))
        acc.unsupported += inner.unsupported
        acc.effects += inner.effects
        return
    if isinstance(node, ast.ClassDef):
        acc.writes[node.name].append((node.lineno, node.col_offset))
        for d in [*node.decorator_list, *node.bases, *[k.value for k in node.keywords]]:
            visit(d, acc)
        inner = Acc()
        for b in node.body:
            visit(b, inner)
        for n in inner.reads:
            if n not in inner.writes:
                acc.reads[n].append((node.lineno, node.col_offset))
        return
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        inner = Acc()
        for child in ast.iter_child_nodes(node):
            visit(child, inner)
        for n, ps_ in inner.reads.items():
            if n not in inner.writes:
                acc.reads[n].extend(ps_)
        acc.effects += inner.effects
        acc.unsupported += inner.unsupported
        return
    if isinstance(node, ast.Name):
        pos = (node.lineno, node.col_offset)
        if isinstance(node.ctx, ast.Load):
            acc.reads[node.id].append(pos)
        elif isinstance(node.ctx, ast.Store):
            acc.writes[node.id].append(pos)
        else:
            acc.deleted.add(node.id)
            acc.writes[node.id].append(pos)
        return
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        acc.decls.update(node.names)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            acc.writes[(a.asname or a.name.split(".")[0])].append((node.lineno, node.col_offset))
    elif isinstance(node, ast.ExceptHandler) and node.name:
        acc.writes[node.name].append((node.lineno, node.col_offset))
    elif isinstance(node, ast.Raise):
        acc.raises.append(node.lineno)
    elif isinstance(node, ast.Match):
        acc.unsupported.append((node.lineno, "match statement"))
    elif isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name):
            if f.id in IO_CALLS:
                acc.effects.append((node.lineno, f"calls {f.id}() (I/O or dynamic execution)"))
        elif isinstance(f, ast.Attribute):
            base = f.value
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name) and base.id in IO_MODULES:
                acc.effects.append((node.lineno, f"calls {base.id}.{f.attr}() (external effect)"))
    elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
        tg = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in tg:
            if isinstance(t, (ast.Attribute, ast.Subscript)):
                acc.effects.append((node.lineno, "mutates an attribute or item of an existing object"))
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            acc.reads[node.target.id].append((node.lineno, node.col_offset))
    for child in ast.iter_child_nodes(node):
        visit(child, acc)


def _definite(stmts: list[ast.stmt]) -> set[str]:
    out: set[str] = set()
    for s in stmts:
        if isinstance(s, ast.Assign):
            for t in s.targets:
                out.update(_targets(t))
        elif isinstance(s, ast.AnnAssign) and s.value is not None:
            out.update(_targets(s.target))
        elif isinstance(s, (ast.Import, ast.ImportFrom)):
            out.update((a.asname or a.name.split(".")[0]) for a in s.names)
        elif isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(s.name)
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            for it in s.items:
                if it.optional_vars is not None:
                    out.update(_targets(it.optional_vars))
            out |= _definite(s.body)
    return out


def _jumps_escape(stmts: list[ast.stmt]) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []

    def go(n: ast.AST, in_loop: bool) -> None:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return
        if isinstance(n, (ast.Return, ast.Yield, ast.YieldFrom, ast.Await)):
            found.append((type(n).__name__.lower(), n.lineno))
        if isinstance(n, (ast.Break, ast.Continue)) and not in_loop:
            found.append((type(n).__name__.lower(), n.lineno))
        loop_child = in_loop or isinstance(n, (ast.For, ast.AsyncFor, ast.While))
        if isinstance(n, (ast.For, ast.AsyncFor, ast.While)):
            for c in n.body:
                go(c, True)
            for c in n.orelse:  # break/continue in a loop's else clause belong to the *outer* loop
                go(c, in_loop)
            if isinstance(n, (ast.For, ast.AsyncFor)):
                go(n.iter, in_loop)
            return
        for c in ast.iter_child_nodes(n):
            go(c, loop_child)

    for s in stmts:
        go(s, False)
    return found


def _find_list(tree: ast.Module, start: int, end: int) -> tuple[list[ast.stmt], int, int, ast.AST] | None:
    best: tuple[list[ast.stmt], int, int, ast.AST] | None = None
    best_span = -1
    for node in ast.walk(tree):
        for fld in ("body", "orelse", "finalbody"):
            lst = getattr(node, fld, None)
            if not isinstance(lst, list) or not lst or not isinstance(lst[0], ast.stmt):
                continue
            idx = [i for i, s in enumerate(lst) if _first(s) >= start and (s.end_lineno or s.lineno) <= end]
            if idx and idx == list(range(idx[0], idx[-1] + 1)):
                span = (lst[idx[-1]].end_lineno or 0) - _first(lst[idx[0]])
                if best is None or span > best_span:  # the list that covers the widest part of the selection
                    best, best_span = (lst, idx[0], idx[-1], node), span
    return best


def _first(s: ast.stmt) -> int:
    return min([s.lineno, *[d.lineno for d in getattr(s, "decorator_list", [])]])


def _string_lines(text: str) -> set[int]:
    inside: set[int] = set()
    try:
        for t in tokenize.generate_tokens(io.StringIO(text).readline):
            if t.type == tokenize.STRING and t.start[0] != t.end[0]:
                inside.update(range(t.start[0] + 1, t.end[0] + 1))
    except (tokenize.TokenError, IndentationError):
        pass
    return inside


def plan_extract(ws: Workspace, rel_path: str, start: int, end: int, new_name: str) -> PlanResult:
    res = PlanResult()
    res.limitations = ["Python only; parameters and results are not type-annotated.", "Exception tracebacks gain one frame; code that inspects frames, locals() or the call stack is blocked.",
                       "Only whole statements inside a function or method are extracted."]
    if not new_name.isidentifier() or keyword.iskeyword(new_name):
        res.add("blocker", "invalid_name", f"{new_name!r} is not a valid function name.")
        return res
    got = ws.read(rel_path)
    if got is None or ws.indexed.get(rel_path, ("",))[0] != "python":
        res.add("blocker", "unsupported_language", "Extract function is implemented for indexed Python files only.")
        return res
    text = got[0]
    if end < start or start < 1:
        res.add("blocker", "invalid_range", "The line range is invalid.")
        return res
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        res.add("blocker", "syntax_error", f"The file does not parse (line {exc.lineno}).")
        return res
    lines = text.split("\n")
    if end > len(lines):
        res.add("blocker", "invalid_range", "The range extends past the end of the file.")
        return res
    found = _find_list(tree, start, end)
    if found is None:
        res.add("blocker", "region_not_statement_aligned", "The selection does not consist of whole, consecutive statements.")
        return res
    lst, i0, i1, container = found
    region = lst[i0:i1 + 1]
    r_start, r_end = _first(region[0]), region[-1].end_lineno or region[-1].lineno
    for ln in list(range(start, r_start)) + list(range(r_end + 1, end + 1)):
        s = lines[ln - 1].strip()
        if s and not s.startswith("#"):
            res.add("blocker", "region_not_statement_aligned", f"Line {ln} is in the selection but is not part of a whole statement.", [loc(rel_path, ln)])
            return res
    # --- enclosing function and its placement
    parents: dict[int, ast.AST] = {}
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            parents[id(c)] = p
    chain: list[ast.AST] = []
    cur: ast.AST | None = container
    while cur is not None:
        chain.append(cur)
        cur = parents.get(id(cur))
    funcs = [c for c in chain if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if not funcs:
        res.add("blocker", "module_level_region", "The region is not inside a function or method; extracting module-level statements changes their scope.")
        return res
    func = funcs[0]
    owner = parents.get(id(func))
    is_method = isinstance(owner, ast.ClassDef)
    if len(funcs) > 1 or not isinstance(owner, (ast.Module, ast.ClassDef)):
        res.add("blocker", "nested_function_context", "The region is inside a nested function; closure variables from the outer function cannot be passed safely.")
    decos = {d.id if isinstance(d, ast.Name) else getattr(d, "attr", "") for d in func.decorator_list}
    first_param = (func.args.posonlyargs + func.args.args)[0].arg if (func.args.posonlyargs + func.args.args) else None
    kind = "function"
    if is_method:
        if "staticmethod" in decos:
            res.add("blocker", "static_method_context", "Extracting from a staticmethod is not supported.")
        elif "classmethod" in decos:
            kind = "classmethod"
        elif first_param:
            kind = "method"
        else:
            res.add("blocker", "method_without_self", "The enclosing method has no self parameter.")
    # --- control flow blockers
    for what, ln in _jumps_escape(region):
        res.add("blocker", "control_flow_" + what, f"The region contains `{what}` (line {ln}) which would change meaning inside a new function.", [loc(rel_path, ln)])
    rinfo = Acc()
    for s in region:
        visit(s, rinfo)
    for ln, what in rinfo.unsupported:
        res.add("blocker", "unsupported_syntax", f"The region uses a {what}, which this analysis does not model.", [loc(rel_path, ln)])
    for d in sorted(rinfo.decls):
        res.add("blocker", "global_or_nonlocal", f"The region declares or uses global/nonlocal {d!r}.")
    if rinfo.deleted:
        res.add("blocker", "deletes_variable", f"The region deletes variable(s) {sorted(rinfo.deleted)}; this cannot be reproduced across a call boundary.")
    dyn = sorted(n for n in rinfo.reads if n in DYNAMIC)
    if dyn:
        res.add("blocker", "dynamic_scope_access", f"The region calls {dyn}, which depends on the local scope.")
    if "__class__" in rinfo.reads or "super" in rinfo.reads:
        res.add("blocker", "uses_super", "The region uses super() or __class__, which needs the enclosing class cell.")
    # --- data flow
    facc = Acc()
    for s in func.body:
        visit(s, facc)
    fparams = _args_names(func.args)
    f_locals = set(fparams) | set(facc.writes)
    for d in facc.decls:
        f_locals.discard(d)
    in_region = lambda pos: r_start <= pos[0] <= r_end  # noqa: E731
    self_name = first_param if kind in ("method", "classmethod") else None
    params: list[str] = []
    possibly_unbound: list[str] = []
    for n in sorted(rinfo.reads, key=lambda k: min(rinfo.reads[k])):
        if n not in f_locals or n == self_name or n in facc.decls:
            continue
        before = [p for p in facc.writes.get(n, []) if p[0] < r_start]
        first_read = min(rinfo.reads[n])
        first_write = min((p for p in rinfo.writes.get(n, [])), default=None)
        if n in fparams or before:
            params.append(n)
        elif first_write is not None and first_write <= first_read:
            continue
        else:
            params.append(n)
            possibly_unbound.append(n)
    for n in possibly_unbound:
        res.add("warning", "possibly_unbound", f"{n!r} is read in the region but is not assigned before it; the original raises UnboundLocalError on that path and so will the call.")
    for ln, free in rinfo.closures:
        rebound = sorted(n for n in free if n in params and any(not in_region(p) for p in facc.writes.get(n, [])))
        if rebound:
            res.add("blocker", "closure_captures_rebound_variable", f"A nested function/lambda captures {rebound}, which the enclosing function rebinds elsewhere; late binding would change.",
                    [loc(rel_path, ln)])
    # loop liveness
    loops = [c for c in chain if isinstance(c, (ast.For, ast.AsyncFor, ast.While)) and c is not func and c.lineno < r_start and (c.end_lineno or 0) >= r_end]
    loop_range = (loops[0].lineno, loops[0].end_lineno or loops[0].lineno) if loops else None
    definite = _definite(region)
    returns: list[str] = []
    dead: list[str] = []
    for n in sorted(rinfo.writes, key=lambda k: min(rinfo.writes[k])):
        if n not in f_locals or n in facc.decls:
            continue
        live = any(p[0] > r_end for p in facc.reads.get(n, [])) or (
            loop_range is not None and any(loop_range[0] <= p[0] <= loop_range[1] and not in_region(p) for p in facc.reads.get(n, [])))
        if not live:
            dead.append(n)
            continue
        if n not in definite and n not in params:
            res.add("blocker", "conditional_assignment", f"{n!r} is assigned only conditionally in the region but used afterwards; returning it could raise or change behavior.")
        returns.append(n)
    # --- name collisions
    taken: set[str] = set(facc.writes) | set(fparams) | set(rinfo.reads)
    module_names = {s.name for s in tree.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    for s in tree.body:
        if isinstance(s, ast.Assign):
            module_names.update(n for t in s.targets for n in _targets(t))
        elif isinstance(s, (ast.Import, ast.ImportFrom)):
            module_names.update((a.asname or a.name.split(".")[0]) for a in s.names)
    scope_names = {s.name for s in (owner.body if isinstance(owner, ast.ClassDef) else tree.body) if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    if new_name in scope_names or (not is_method and new_name in module_names):
        res.add("blocker", "name_conflict", f"{new_name!r} is already defined in the target scope.")
    if new_name in taken and not is_method:
        res.add("blocker", "name_conflict", f"{new_name!r} is already used as a variable in the enclosing function; the call would be shadowed.")
    # --- report
    raises = sorted(set(rinfo.raises))
    effects = sorted({f"line {ln}: {t}" for ln, t in rinfo.effects})
    uses_global = sorted(n for n in rinfo.reads if n not in f_locals and n in module_names)
    res.side_effects = effects
    if raises:
        res.add("info", "raises_exceptions", f"The region raises exceptions (lines {raises[:5]}); they propagate to the caller unchanged, with one extra traceback frame.")
    if uses_global:
        res.add("info", "reads_module_globals", f"The extracted code reads module-level names {uses_global[:8]}; they remain visible because the function stays in the same module.")
    if effects:
        res.add("info", "side_effects", f"{len(effects)} side effect(s) are preserved in order but now happen inside the new function.")
    ret_expr = ", ".join(returns)
    call_args = ", ".join(params)
    sig = ", ".join(([self_name] if self_name else []) + params)
    callee = {"function": new_name, "method": f"{self_name}.{new_name}", "classmethod": f"{self_name}.{new_name}"}[kind]
    call = f"{callee}({call_args})"
    base_indent = lines[r_start - 1][: len(lines[r_start - 1]) - len(lines[r_start - 1].lstrip())]
    call_line = base_indent + (f"{ret_expr} = {call}" if returns else call)
    res.extra = {"function": {"name": getattr(func, "name", ""), "line": func.lineno, "kind": kind}, "new_function": new_name, "parameters": params, "returns": returns,
                 "unused_assignments": dead, "raises": raises, "region": {"start": r_start, "end": r_end}, "call": call_line.strip(),
                 "placement": ("class" if is_method else "module"), "analysis": {"reads": sorted(rinfo.reads), "writes": sorted(rinfo.writes)}}
    res.resolution = {"rel_path": rel_path, "region": [r_start, r_end], "enclosing": func.name, "kind": kind}
    if res.blockers:
        return res
    # --- generate
    f_col = func.col_offset
    f_indent = " " * f_col
    inner_indent = f_indent + "    "
    strlines = _string_lines("\n".join(lines))
    body: list[str] = []
    for ln in range(r_start, r_end + 1):
        raw = lines[ln - 1]
        if ln in strlines:
            body.append(raw)
        elif not raw.strip():
            body.append("")
        elif raw.startswith(base_indent):
            body.append(inner_indent + raw[len(base_indent):])
        else:
            res.add("blocker", "inconsistent_indentation", f"Line {ln} is indented less than the region.", [loc(rel_path, ln)])
            return res
    if returns:
        body.append(inner_indent + f"return {ret_expr}")
    header = ([f_indent + "@classmethod"] if kind == "classmethod" else []) + [f_indent + f"def {new_name}({sig}):"]
    new_func = [*header, *body]
    f_end = func.end_lineno or func.lineno
    blank = [""] if is_method else ["", ""]
    new_lines = lines[: r_start - 1] + [call_line] + lines[r_end:f_end] + blank + new_func + lines[f_end:]
    new_text = "\n".join(new_lines)
    try:
        ast.parse(new_text)
    except SyntaxError as exc:
        res.add("blocker", "generated_code_invalid", f"The generated code does not parse (line {exc.lineno}); no change is offered.")
        return res
    before, after = ps.analyze(rel_path, text), ps.analyze(rel_path, new_text)
    if before is None or after is None:
        res.add("blocker", "scope_analysis_failed", "The result could not be analysed.")
        return res
    new_undefined = ps.undefined_names(after) - ps.undefined_names(before)
    if new_undefined:
        res.add("blocker", "undefined_names_introduced", f"The extraction would leave undefined name(s) {sorted(new_undefined)}; no change is offered.")
        return res
    res.rewrites = {rel_path: new_text}
    res.block_edits = [
        {"rel_path": rel_path, "kind": "replace_region", "start_line": r_start, "end_line": r_end, "old": scrub("\n".join(lines[r_start - 1:r_end]), 1500), "new": call_line,
         "note": "Region replaced by a call."},
        {"rel_path": rel_path, "kind": "insert_function", "start_line": f_end + 1, "end_line": f_end + len(new_func), "old": "", "new": scrub("\n".join(new_func), 1500),
         "note": f"New {kind} {new_name}."}]
    return res
