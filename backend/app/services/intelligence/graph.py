"""Pure graph algorithms used by dependency, impact and cycle analysis. No database or filesystem access.

Complexity (V nodes, E edges): bounded BFS O(visited + edges examined), Tarjan SCC O(V + E),
longest-path-on-condensation O(V + E). All results are deterministic (sorted iteration, no hash-order dependence).
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

Edge = tuple[str, str, dict[str, Any]]  # (from_node, to_node, metadata)


@dataclass
class Visit:
    depth: int
    parent: str | None
    edge: dict[str, Any]


@dataclass
class BfsResult:
    visited: dict[str, Visit] = field(default_factory=dict)  # excludes the seeds
    truncated: bool = False  # stopped because max_nodes was reached
    more_beyond_depth: bool = False  # nodes exist past max_depth that were not returned

    def path_to(self, node: str, seeds: Iterable[str] = ()) -> list[str]:
        seeds_set = set(seeds)
        out = [node]
        cur = self.visited[node].parent
        guard = 0
        while cur is not None and guard < 10_000:
            out.append(cur)
            if cur in seeds_set or cur not in self.visited:
                break
            cur = self.visited[cur].parent
            guard += 1
        return list(reversed(out))


def bfs(seeds: list[str], expand: Callable[[list[str]], list[Edge]], max_depth: int, max_nodes: int) -> BfsResult:
    """Breadth-first traversal from `seeds`, expanding a whole frontier per call (one query per level).

    Cycles cannot cause infinite traversal (each node is visited once). Nodes are deduplicated: a node reached by
    several paths is reported once, at its shortest depth, via the lexicographically smallest discovering edge.
    """
    result = BfsResult()
    seen = set(seeds)
    frontier = sorted(seeds)
    depth = 0
    while frontier:
        edges = sorted(expand(frontier), key=lambda e: (e[1], e[0], str(e[2].get("type", ""))))
        if depth >= max_depth:
            result.more_beyond_depth = any(e[1] not in seen for e in edges)
            break
        nxt: list[str] = []
        for src, dst, meta in edges:
            if dst in seen:
                continue
            if len(result.visited) >= max_nodes:
                result.truncated = True
                return result
            seen.add(dst)
            result.visited[dst] = Visit(depth + 1, src, meta)
            nxt.append(dst)
        frontier = sorted(nxt)
        depth += 1
    return result


def tarjan_scc(nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> list[list[str]]:
    """Strongly connected components (iterative Tarjan). Components are emitted in reverse topological order
    of the condensation: a component appears after every component it can reach."""
    adj: dict[str, list[str]] = {}
    node_list = sorted(set(nodes))
    for n in node_list:
        adj[n] = []
    for a, b in edges:
        if a in adj and b in adj:
            adj[a].append(b)
    for n in adj:
        adj[n] = sorted(set(adj[n]))
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    comps: list[list[str]] = []
    counter = 0
    for root in node_list:
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            v, i = work.pop()
            if i == 0:
                index[v] = low[v] = counter
                counter += 1
                stack.append(v)
                on_stack.add(v)
            recurse = False
            nbrs = adj[v]
            while i < len(nbrs):
                w = nbrs[i]
                i += 1
                if w not in index:
                    work.append((v, i))
                    work.append((w, 0))
                    recurse = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if recurse:
                continue
            if low[v] == index[v]:
                comp: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                comps.append(sorted(comp))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
    return comps


def find_cycles(nodes: Iterable[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    """Structural cycles: components with more than one node, or a single node that imports itself."""
    selfloops = {a for a, b in edges if a == b}
    out = [c for c in tarjan_scc(nodes, edges) if len(c) > 1 or c[0] in selfloops]
    return sorted(out, key=lambda c: (-len(c), c))


def max_dependency_depth(nodes: Iterable[str], edges: list[tuple[str, str]]) -> int:
    """Length (in edges) of the longest dependency chain after collapsing each cycle into one node."""
    comps = tarjan_scc(nodes, edges)
    comp_of = {n: i for i, c in enumerate(comps) for n in c}
    succ: dict[int, set[int]] = {i: set() for i in range(len(comps))}
    for a, b in edges:
        if a in comp_of and b in comp_of and comp_of[a] != comp_of[b]:
            succ[comp_of[a]].add(comp_of[b])
    depth: dict[int, int] = {}
    for i in range(len(comps)):  # reverse topological order: successors already computed
        depth[i] = 1 + max((depth[j] for j in succ[i]), default=-1)
    return max(depth.values(), default=0)


def cycle_id(paths: list[str]) -> str:
    return hashlib.sha1("\n".join(sorted(paths)).encode()).hexdigest()[:12]  # noqa: S324 (identifier, not security)


def cycle_severity(size: int, self_import: bool) -> tuple[str, str]:
    """Documented rule: self-import -> info; 2-3 files -> medium; 4+ files -> high.
    These describe structural coupling, not a confirmed runtime failure."""
    if self_import and size == 1:
        return "info", "file imports itself"
    if size <= 3:
        return "medium", f"{size} files import each other (directly or transitively)"
    return "high", f"{size} files are mutually reachable through imports"
