"""Conservative, name-based call resolution.

A call site is *observed* syntax. Its target is *inferred* from names, same-file scope, direct import bindings and
simple inheritance, never from types or execution. Anything that cannot be tied to a definition by those rules is
left `unresolved` (or `unsupported` for expressions NOVA does not interpret), and name collisions are `ambiguous`
rather than silently picking one. No code is executed and nothing is evaluated.
"""
from __future__ import annotations

from dataclasses import dataclass, field

RESOLVED, AMBIGUOUS, UNRESOLVED, UNSUPPORTED, EXTERNAL = "resolved", "ambiguous", "unresolved", "unsupported", "external"
CALLABLE_KINDS = frozenset({"function", "class"})
SELF_NAMES = frozenset({"self", "cls", "this"})
MAX_CANDIDATES = 5
MAX_BASE_DEPTH = 5


@dataclass(frozen=True)
class SymRef:
    stable_id: str
    file_id: str
    name: str
    kind: str
    parent: str | None


@dataclass(frozen=True)
class Binding:
    kind: str  # "sym" (named import), "mod" (module/namespace alias), "ext" (external package), "default"
    file_id: str | None = None
    name: str | None = None
    package: str | None = None


@dataclass
class CallContext:
    by_file_name: dict[tuple[str, str], list[SymRef]] = field(default_factory=dict)
    members: dict[tuple[str, str], list[SymRef]] = field(default_factory=dict)  # (class stable id, name)
    syms: dict[str, SymRef] = field(default_factory=dict)  # stable id -> symbol
    bases: dict[str, list[str]] = field(default_factory=dict)  # class stable id -> base names (text)
    bindings: dict[tuple[str, str], Binding] = field(default_factory=dict)  # (file id, local name)

    def add(self, s: SymRef, bases: list[str] | None) -> None:
        self.syms[s.stable_id] = s
        parent = self.syms.get(s.parent) if s.parent else None
        if parent is not None and parent.kind == "class":
            self.members.setdefault((parent.stable_id, s.name), []).append(s)
        if s.kind in CALLABLE_KINDS:
            self.by_file_name.setdefault((s.file_id, s.name), []).append(s)
        if bases and s.kind == "class":
            self.bases[s.stable_id] = bases


@dataclass
class CallResolution:
    status: str
    note: str = ""
    targets: list[SymRef] = field(default_factory=list)


def _outcome(cands: list[SymRef], note: str) -> CallResolution:
    if len(cands) == 1:
        return CallResolution(RESOLVED, note, cands)
    if len(cands) > 1:
        return CallResolution(AMBIGUOUS, f"{len(cands)} candidates{'; ' + note if note else ''}",
                              sorted(cands, key=lambda c: (c.file_id, c.stable_id))[:MAX_CANDIDATES])
    return CallResolution(UNRESOLVED, note)


def _ancestors(ctx: CallContext, caller: SymRef | None) -> list[SymRef]:
    chain: list[SymRef] = []
    cur = caller
    seen: set[str] = set()
    while cur is not None and cur.stable_id not in seen:
        chain.append(cur)
        seen.add(cur.stable_id)
        cur = ctx.syms.get(cur.parent) if cur.parent else None
    return chain


def _class_lookup(ctx: CallContext, file_id: str, name: str) -> list[SymRef]:
    return [s for s in ctx.by_file_name.get((file_id, name), []) if s.kind == "class"]


def _method_on(ctx: CallContext, cls: SymRef, method: str, depth: int = 0, seen: set[str] | None = None
               ) -> tuple[list[SymRef], bool]:
    """Find `method` on a class, following base classes bound in the same file or by direct import."""
    seen = seen if seen is not None else set()
    if cls.stable_id in seen or depth > MAX_BASE_DEPTH:
        return [], False
    seen.add(cls.stable_id)
    direct = ctx.members.get((cls.stable_id, method), [])
    if direct:
        return list(direct), depth > 0
    for base in ctx.bases.get(cls.stable_id, []):
        head = base.split("(")[0].split("<")[0].strip()
        if "." in head:
            continue
        found = _class_lookup(ctx, cls.file_id, head)
        if not found:
            b = ctx.bindings.get((cls.file_id, head))
            if b is not None and b.kind == "sym" and b.file_id and b.name:
                found = _class_lookup(ctx, b.file_id, b.name)
        if len(found) == 1:
            res, _ = _method_on(ctx, found[0], method, depth + 1, seen)
            if res:
                return res, True
    return [], False


def resolve_call(ctx: CallContext, file_id: str, caller: SymRef | None, callee: str | None) -> CallResolution:
    if not callee:
        return CallResolution(UNSUPPORTED, "callee is not a simple name (call result, subscript or other expression)")
    parts = callee.split(".")
    if len(parts) > 2:
        return CallResolution(UNSUPPORTED, "multi-level attribute access is not resolved")
    chain = _ancestors(ctx, caller)
    chain_ids = {c.stable_id for c in chain}

    if len(parts) == 1:
        name = parts[0]
        local = [s for s in ctx.by_file_name.get((file_id, name), [])
                 if s.parent is None or s.parent in chain_ids]
        if local:
            return _outcome(local, "same-file definition")
        b = ctx.bindings.get((file_id, name))
        if b is None:
            return CallResolution(UNRESOLVED, "no definition or import binding found (may be a builtin)")
        if b.kind == "ext":
            return CallResolution(EXTERNAL, f"imported from package {b.package}" if b.package else "external import")
        if b.kind == "sym" and b.file_id and b.name:
            cands = ctx.by_file_name.get((b.file_id, b.name), [])
            if cands:
                return _outcome(list(cands), "direct import binding")
            return CallResolution(UNRESOLVED, "imported name not defined in target file (re-export?)")
        if b.kind == "default":
            return CallResolution(UNRESOLVED, "default-import target is not tracked")
        return CallResolution(UNRESOLVED, "name is bound to a module, not a callable")

    head, attr = parts
    if head in SELF_NAMES:
        cls = next((c for c in chain if c.kind == "class"), None)
        if cls is None:
            return CallResolution(UNRESOLVED, "no enclosing class for self/this")
        found, inherited = _method_on(ctx, cls, attr)
        if found:
            return _outcome(found, "via inheritance" if inherited else "method of enclosing class")
        return CallResolution(UNRESOLVED, "method not found on enclosing class (dynamic or inherited from unknown base)")
    b = ctx.bindings.get((file_id, head))
    if b is not None:
        if b.kind == "ext":
            return CallResolution(EXTERNAL, f"attribute of package {b.package}" if b.package else "external import")
        if b.kind == "mod" and b.file_id:
            cands = ctx.by_file_name.get((b.file_id, attr), [])
            return _outcome(list(cands), "module alias") if cands else CallResolution(
                UNRESOLVED, "name not defined in the imported module (re-export?)")
        if b.kind == "sym" and b.file_id and b.name:
            classes = _class_lookup(ctx, b.file_id, b.name)
            if len(classes) == 1:
                found, _ = _method_on(ctx, classes[0], attr)
                if found:
                    return _outcome(found, "method of imported class")
    classes = _class_lookup(ctx, file_id, head)
    if len(classes) == 1:
        found, _ = _method_on(ctx, classes[0], attr)
        if found:
            return _outcome(found, "method of same-file class")
    return CallResolution(UNRESOLVED, "receiver type is not statically known")
