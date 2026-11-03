"""Scope analysis for Python source, built on the standard ``ast`` module.

The Phase 03 index records definitions and imports but not name *references*, so cross-file renames need their own, scope-aware view
of every occurrence of a name. This module builds a scope tree for one file and reports, for each name token, which binding it
refers to (module, function, class, lambda or comprehension scope). It never executes code.

Python rules implemented: a name bound anywhere in a function is local to the whole function; class scopes are not visible to
nested functions; ``global``/``nonlocal`` redirect a name; the first iterable of a comprehension is evaluated outside it; ``:=`` in a
comprehension binds in the enclosing scope; decorators, defaults and annotations belong to the enclosing scope.

Not implemented (reported through ``FileAnalysis.unsupported`` so callers can refuse instead of guess): ``match`` captures, PEP 695
type parameters, ``exec``/``eval``/``locals()`` style dynamic scope access is *detected* and reported as ``dynamic_scope``.
"""
from __future__ import annotations

import ast
import builtins
import io
import tokenize
from dataclasses import dataclass, field

BUILTINS = frozenset(dir(builtins))


@dataclass(eq=False)
class Scope:
    id: int
    kind: str  # module | function | class | lambda | comprehension
    node: ast.AST
    parent: Scope | None
    name: str = ""
    bound: set[str] = field(default_factory=set)
    globals_: set[str] = field(default_factory=set)
    nonlocals: set[str] = field(default_factory=set)

    @property
    def qualified(self) -> str:
        parts: list[str] = []
        cur: Scope | None = self
        while cur is not None and cur.kind != "module":
            if cur.name:
                parts.append(cur.name)
            cur = cur.parent
        return ".".join(reversed(parts))


@dataclass
class Site:
    """A name token. ``col``/``end_col`` are character offsets (not bytes) on a single line; ``None`` positions are unknown."""

    name: str
    line: int
    col: int
    end_col: int
    kind: str  # def | class | import | param | assign | for | with | except | global | nonlocal | use | del | walrus | match
    scope: Scope
    resolved: Scope | None = None  # scope whose binding this token refers to (None: builtin/undefined)
    node: ast.AST | None = None
    extra: dict[str, object] = field(default_factory=dict)


@dataclass
class ImportBinding:
    """A name introduced by an import statement."""

    local: str
    scope: Scope
    module: str  # absolute dotted module the statement names (after resolving relative levels, if possible; '' when unknown)
    symbol: str | None  # imported name for ``from m import symbol``; None for ``import m``
    alias: str | None
    site: Site | None  # token of the local name's binding
    name_site: Site | None  # token of the imported symbol (``from m import symbol``) or of the module's first segment
    level: int = 0
    raw_module: str = ""
    star: bool = False
    line: int = 0


@dataclass
class FileAnalysis:
    rel_path: str
    source: str
    lines: list[str]
    tree: ast.Module
    scopes: list[Scope]
    module_scope: Scope
    sites: list[Site]
    imports: list[ImportBinding]
    attributes: list[tuple[ast.Attribute, Scope, Site]]  # attribute tokens (the attr name)
    string_annotations: list[tuple[ast.Constant, Scope]]
    all_exports: list[tuple[str, int, int, int]]  # names listed in a literal __all__: (name, line, col, end_col)
    unsupported: list[str]
    dynamic: list[str]
    star_imports: list[ImportBinding]

    def resolve(self, scope: Scope, name: str) -> Scope | None:
        return resolve(scope, name)


def resolve(scope: Scope, name: str) -> Scope | None:
    """The scope whose binding ``name`` refers to when used inside ``scope`` (None: builtin or undefined)."""
    cur: Scope | None = scope
    first = True
    while cur is not None:
        if name in cur.globals_:
            return _module(cur)
        if name in cur.nonlocals:
            cur = cur.parent
            first = False
            while cur is not None and (cur.kind in ("class", "module") or name not in cur.bound):
                cur = cur.parent
            return cur
        if (first or cur.kind != "class") and name in cur.bound:
            return cur
        first = False
        cur = cur.parent
    return None


def _module(scope: Scope) -> Scope:
    cur = scope
    while cur.parent is not None:
        cur = cur.parent
    return cur


def char_col(line: str, byte_col: int) -> int:
    return len(line.encode("utf-8")[:byte_col].decode("utf-8", errors="ignore"))


class _Tokens:
    """NAME tokens by line, to locate names that the AST stores only as strings (def/class names, aliases, except-as, global)."""

    def __init__(self, source: str) -> None:
        self.by_line: dict[int, list[tuple[str, int, int]]] = {}
        try:
            for tok in tokenize.generate_tokens(io.StringIO(source).readline):
                if tok.type == tokenize.NAME:
                    self.by_line.setdefault(tok.start[0], []).append((tok.string, tok.start[1], tok.end[1]))
        except (tokenize.TokenError, IndentationError, SyntaxError):
            pass

    def find(self, name: str, line: int, from_col: int = 0, to_line: int | None = None) -> tuple[int, int, int] | None:
        for ln in range(line, (to_line or line) + 1):
            for s, a, b in self.by_line.get(ln, []):
                if s == name and (ln > line or a >= from_col):
                    return ln, a, b
        return None

    def after(self, keyword: str, line: int, from_col: int, name: str) -> tuple[int, int, int] | None:
        """The first NAME == ``name`` that follows the NAME ``keyword`` on ``line``."""
        seen = False
        for s, a, b in self.by_line.get(line, []):
            if a < from_col:
                continue
            if seen and s == name:
                return line, a, b
            if s == keyword:
                seen = True
        return None


class _Builder(ast.NodeVisitor):
    def __init__(self, rel: str, source: str, tree: ast.Module) -> None:
        self.rel = rel
        self.lines = source.split("\n")
        self.tokens = _Tokens(source)
        self.scopes: list[Scope] = []
        self.sites: list[Site] = []
        self.imports: list[ImportBinding] = []
        self.attrs: list[tuple[ast.Attribute, Scope, Site]] = []
        self.str_ann: list[tuple[ast.Constant, Scope]] = []
        self.unsupported: list[str] = []
        self.dynamic: list[str] = []
        self.all_exports: list[tuple[str, int, int, int]] = []
        self.module = self._new("module", tree, None, "")
        self.scope = self.module
        self.pkg_parts = self._package(rel)
        self._in_annotation = 0

    # --- helpers
    @staticmethod
    def _package(rel: str) -> list[str]:
        parts = rel[:-3].split("/") if rel.endswith(".py") else rel.split("/")
        return parts[:-1] if parts and parts[-1] != "__init__" else parts[:-1]

    def _new(self, kind: str, node: ast.AST, parent: Scope | None, name: str) -> Scope:
        s = Scope(len(self.scopes), kind, node, parent, name)
        self.scopes.append(s)
        return s

    def _col(self, line: int, byte_col: int) -> int:
        return char_col(self.lines[line - 1], byte_col) if 0 < line <= len(self.lines) else byte_col

    def _site(self, name: str, line: int, col: int, end_col: int, kind: str, scope: Scope | None = None, node: ast.AST | None = None) -> Site:
        s = Site(name, line, col, end_col, kind, scope or self.scope, node=node)
        self.sites.append(s)
        return s

    def _bind(self, name: str, scope: Scope | None = None) -> None:
        sc = scope or self.scope
        if name in sc.globals_:
            _module(sc).bound.add(name)
        elif name in sc.nonlocals:
            return
        else:
            sc.bound.add(name)

    def _binding_scope_for_walrus(self) -> Scope:
        sc = self.scope
        while sc.kind == "comprehension" and sc.parent is not None:
            sc = sc.parent
        return sc

    # --- module and scopes
    def run(self, tree: ast.Module) -> None:
        for stmt in tree.body:
            self.visit(stmt)
        self._collect_all(tree)

    def _collect_all(self, tree: ast.Module) -> None:
        for stmt in tree.body:
            targets = []
            if isinstance(stmt, ast.Assign):
                targets = [t for t in stmt.targets if isinstance(t, ast.Name) and t.id == "__all__"]
                value = stmt.value
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == "__all__" and stmt.value is not None:
                targets, value = [stmt.target], stmt.value
            else:
                continue
            if targets and isinstance(value, (ast.List, ast.Tuple)):
                for el in value.elts:
                    if isinstance(el, ast.Constant) and isinstance(el.value, str) and el.lineno == el.end_lineno:
                        ln = el.lineno
                        a = self._col(ln, el.col_offset)
                        b = self._col(ln, el.end_col_offset)
                        self.all_exports.append((el.value, ln, a + 1, b - 1))  # inside the quotes

    # --- function-like scopes
    def _visit_args(self, args: ast.arguments, scope: Scope) -> None:
        allargs = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        if args.vararg:
            allargs.append(args.vararg)
        if args.kwarg:
            allargs.append(args.kwarg)
        for a in allargs:
            self._bind(a.arg, scope)
            ln = a.lineno
            c = self._col(ln, a.col_offset)
            self._site(a.arg, ln, c, c + len(a.arg), "param", scope, a)

    def _visit_annotation(self, node: ast.expr | None) -> None:
        if node is None:
            return
        self._in_annotation += 1
        self.visit(node)
        self._in_annotation -= 1

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for d in node.decorator_list:
            self.visit(d)
        a = node.args
        for d in [*a.defaults, *[k for k in a.kw_defaults if k is not None]]:
            self.visit(d)
        for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, *([a.vararg] if a.vararg else []), *([a.kwarg] if a.kwarg else [])]:
            self._visit_annotation(arg.annotation)
        self._visit_annotation(node.returns)
        if getattr(node, "type_params", None):
            self.unsupported.append(f"line {node.lineno}: PEP 695 type parameters")
        # the name binds in the enclosing scope
        self._bind(node.name)
        tok = self.tokens.after("def", node.lineno, self._col(node.lineno, node.col_offset), node.name)
        if tok:
            self._site(node.name, tok[0], tok[1], tok[2], "def", node=node)
        fs = self._new("function", node, self.scope, node.name)
        self._visit_args(a, fs)
        outer, self.scope = self.scope, fs
        for stmt in node.body:
            self.visit(stmt)
        self.scope = outer

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_Lambda(self, node: ast.Lambda) -> None:
        a = node.args
        for d in [*a.defaults, *[k for k in a.kw_defaults if k is not None]]:
            self.visit(d)
        ls = self._new("lambda", node, self.scope, "<lambda>")
        self._visit_args(a, ls)
        outer, self.scope = self.scope, ls
        self.visit(node.body)
        self.scope = outer

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for d in node.decorator_list:
            self.visit(d)
        for b in node.bases:
            self.visit(b)
        for k in node.keywords:
            self.visit(k.value)
        if getattr(node, "type_params", None):
            self.unsupported.append(f"line {node.lineno}: PEP 695 type parameters")
        self._bind(node.name)
        tok = self.tokens.after("class", node.lineno, self._col(node.lineno, node.col_offset), node.name)
        if tok:
            self._site(node.name, tok[0], tok[1], tok[2], "class", node=node)
        cs = self._new("class", node, self.scope, node.name)
        outer, self.scope = self.scope, cs
        for stmt in node.body:
            self.visit(stmt)
        self.scope = outer

    def _comprehension(self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp) -> None:
        first = node.generators[0]
        self.visit(first.iter)
        cs = self._new("comprehension", node, self.scope, "<comp>")
        outer, self.scope = self.scope, cs
        for i, g in enumerate(node.generators):
            if i > 0:
                self.visit(g.iter)
            self.visit(g.target)
            for cond in g.ifs:
                self.visit(cond)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
        self.scope = outer

    visit_ListComp = visit_SetComp = visit_GeneratorExp = visit_DictComp = _comprehension

    # --- names
    def visit_Name(self, node: ast.Name) -> None:
        ln = node.lineno
        c = self._col(ln, node.col_offset)
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            kind = "del" if isinstance(node.ctx, ast.Del) else "assign"
            if kind == "assign":
                self._bind(node.id)
            self._site(node.id, ln, c, c + len(node.id), kind, node=node)
        else:
            self._site(node.id, ln, c, c + len(node.id), "use", node=node)
            if node.id in ("locals", "vars", "eval", "exec", "globals", "__import__"):
                self.dynamic.append(f"line {ln}: {node.id}()")

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        target = self._binding_scope_for_walrus()
        self._bind(node.target.id, target)
        ln = node.target.lineno
        c = self._col(ln, node.target.col_offset)
        self._site(node.target.id, ln, c, c + len(node.target.id), "walrus", target, node.target)

    def visit_Global(self, node: ast.Global) -> None:
        for n in node.names:
            self.scope.globals_.add(n)
            _module(self.scope).bound.add(n)
            tok = self.tokens.find(n, node.lineno, self._col(node.lineno, node.col_offset) + 6, node.end_lineno)
            if tok:
                self._site(n, tok[0], tok[1], tok[2], "global")

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        for n in node.names:
            self.scope.nonlocals.add(n)
            tok = self.tokens.find(n, node.lineno, self._col(node.lineno, node.col_offset) + 8, node.end_lineno)
            if tok:
                self._site(n, tok[0], tok[1], tok[2], "nonlocal")

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        if node.name:
            self._bind(node.name)
            tok = self.tokens.after("as", node.lineno, self._col(node.lineno, node.col_offset), node.name)
            if tok:
                self._site(node.name, tok[0], tok[1], tok[2], "except", node=node)
        for stmt in node.body:
            self.visit(stmt)

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        for case in node.cases:
            for sub in ast.walk(case.pattern):
                nm = getattr(sub, "name", None) or getattr(sub, "rest", None)
                if isinstance(nm, str):
                    self._bind(nm)
                    self.unsupported.append(f"line {case.pattern.lineno}: match-statement capture '{nm}'")
            if case.guard is not None:
                self.visit(case.guard)
            for stmt in case.body:
                self.visit(stmt)

    def visit_Import(self, node: ast.Import) -> None:
        for al in node.names:
            local = al.asname or al.name.split(".")[0]
            self._bind(local)
            ln = al.lineno
            c0 = self._col(ln, al.col_offset)
            first = al.name.split(".")[0]
            tok_first = self.tokens.find(first, ln, c0)
            site = name_site = None
            if al.asname:
                t2 = self.tokens.after("as", ln, c0, al.asname)
                if t2:
                    site = self._site(local, t2[0], t2[1], t2[2], "import", node=al)
            elif tok_first:
                site = self._site(local, tok_first[0], tok_first[1], tok_first[2], "import", node=al)
                name_site = site
            self.imports.append(ImportBinding(local, self.scope, al.name, None, al.asname, site, name_site, 0, al.name, False, ln))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        level = node.level or 0
        mod = node.module or ""
        abs_mod = mod
        if level:
            base = self.pkg_parts[: max(0, len(self.pkg_parts) - (level - 1))] if level - 1 <= len(self.pkg_parts) else None
            abs_mod = ".".join([*(base or []), *([mod] if mod else [])]) if base is not None else ""
        for al in node.names:
            ln = al.lineno
            c0 = self._col(ln, al.col_offset)
            if al.name == "*":
                ib = ImportBinding("*", self.scope, abs_mod, "*", None, None, None, level, mod, True, ln)
                self.imports.append(ib)
                continue
            local = al.asname or al.name
            self._bind(local)
            tok = self.tokens.find(al.name, ln, c0)
            name_site = self._site(al.name, tok[0], tok[1], tok[2], "import", node=al) if tok else None
            site = name_site
            if al.asname:
                t2 = self.tokens.after("as", ln, c0, al.asname)
                site = self._site(local, t2[0], t2[1], t2[2], "import", node=al) if t2 else None
            self.imports.append(ImportBinding(local, self.scope, abs_mod, al.name, al.asname, site, name_site, level, mod, False, ln))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.generic_visit(node)
        if node.end_lineno == node.lineno:
            end = self._col(node.end_lineno or node.lineno, node.end_col_offset or 0)
            site = Site(node.attr, node.lineno, end - len(node.attr), end, "attr", self.scope, node=node)
            self.attrs.append((node, self.scope, site))

    def visit_Constant(self, node: ast.Constant) -> None:
        if self._in_annotation and isinstance(node.value, str):
            self.str_ann.append((node, self.scope))

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in ("getattr", "setattr", "hasattr", "delattr") and len(node.args) >= 2:
            a = node.args[1]
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                self.dynamic.append(f"line {node.lineno}: {node.func.id}(..., {a.value!r})")
        self.generic_visit(node)


def analyze(rel_path: str, source: str) -> FileAnalysis | None:
    """Parse and analyse one file. Returns None when the file does not parse."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    b = _Builder(rel_path, source, tree)
    b.run(tree)
    for s in b.sites:
        if s.kind in ("use", "del", "assign", "walrus"):
            s.resolved = resolve(s.scope, s.name)
        elif s.kind in ("def", "class", "param", "import", "except", "global", "nonlocal"):
            s.resolved = resolve(s.scope, s.name) if s.kind in ("global", "nonlocal") else s.scope
            if s.kind in ("def", "class", "import", "except") and s.name in s.scope.globals_:
                s.resolved = _module(s.scope)
    return FileAnalysis(rel_path, source, b.lines, tree, b.scopes, b.module, b.sites, [i for i in b.imports if not i.star], b.attrs, b.str_ann,
                        b.all_exports, b.unsupported, b.dynamic, [i for i in b.imports if i.star])


def undefined_names(fa: FileAnalysis) -> set[str]:
    """Names used but neither bound in any visible scope nor builtin (a pyflakes-style approximation used for before/after checks)."""
    star = bool(fa.star_imports)
    out: set[str] = set()
    for s in fa.sites:
        if s.kind == "use" and s.resolved is None and s.name not in BUILTINS and s.name not in ("__file__", "__name__", "__doc__", "__spec__", "__path__",
                                                                                                 "__builtins__", "__package__", "__loader__"):
            if not star:
                out.add(s.name)
    return out
