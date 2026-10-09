"""The dependency graph: four stored edge sets, one derived, hashes, invariants, analyses."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from .canon import H, canon
from .store import Task

STORED = ("blocks", "unlocks", "informs", "supersedes")


@dataclass
class Graph:
    tasks: dict[str, Task]
    blocks: set[tuple[str, str]] = field(default_factory=set)        # (A, B): A blocks B
    unlocks: set[tuple[str, str]] = field(default_factory=set)       # (A, B): A's report proposed B
    informs: dict[tuple[str, str], dict] = field(default_factory=dict)  # (A, B) -> {weight, reason}
    supersedes: set[tuple[str, str]] = field(default_factory=set)    # (A, B): B replaces A
    conflicts: set[tuple[str, str]] = field(default_factory=set)     # {a,b} as (a,b) a<b, derived

    # ---- construction -----------------------------------------------------
    @classmethod
    def build(cls, tasks: dict[str, Task]) -> "Graph":
        g = cls(tasks)
        for t in tasks.values():
            r = t.rec
            for a in r.get("blocked_by") or []:
                g.blocks.add((a, t.id))
            o = r.get("origin")
            if o and o in tasks:
                g.unlocks.add((o, t.id))
            for rel in r.get("related") or []:
                g.informs[(rel["task"], t.id)] = {"weight": float(rel.get("weight", 0.5)), "reason": rel.get("reason", "")}
            for s in r.get("supersedes") or []:
                g.supersedes.add((s, t.id))
        # conflicts: scope overlap among open tasks
        open_tasks = [t for t in tasks.values() if t.is_open]
        for i, a in enumerate(open_tasks):
            for b in open_tasks[i + 1:]:
                if _scope_overlap(a, b):
                    g.conflicts.add(tuple(sorted((a.id, b.id))))  # type: ignore[arg-type]
        return g

    # ---- hashes -----------------------------------------------------------
    def edge_dicts(self, name: str) -> list[dict]:
        if name == "informs":
            es = [{"from": a, "to": b, **v} for (a, b), v in self.informs.items()]
        elif name == "conflicts":
            es = [{"a": a, "b": b} for a, b in self.conflicts]
        else:
            es = [{"from": a, "to": b} for a, b in getattr(self, name)]
        return sorted(es, key=canon)

    def edge_set_hash(self, name: str) -> str:
        return H("graph/" + name, *(canon(e) for e in self.edge_dicts(name)))

    def hashes(self) -> dict[str, str]:
        hs = {n: self.edge_set_hash(n) for n in (*STORED, "conflicts")}
        hs["graph_root"] = H("graph", *(hs[n] for n in (*STORED, "conflicts")))
        return hs

    def to_json(self) -> dict:
        return {
            "hashes": self.hashes(),
            "nodes": {t.id: {"status": t.status, "kind": t.rec.get("kind"), "queue": t.rec.get("queue"), "title": t.rec.get("title")} for t in self.tasks.values()},
            "edges": {n: self.edge_dicts(n) for n in (*STORED, "conflicts")},
        }

    # ---- adjacency helpers -----------------------------------------------
    def succ(self, edges: set[tuple[str, str]]) -> dict[str, list[str]]:
        d: dict[str, list[str]] = defaultdict(list)
        for a, b in edges:
            d[a].append(b)
        return d

    def pred(self, edges: set[tuple[str, str]]) -> dict[str, list[str]]:
        d: dict[str, list[str]] = defaultdict(list)
        for a, b in edges:
            d[b].append(a)
        return d

    def descendants(self, tid: str, edges: set[tuple[str, str]] | None = None) -> set[str]:
        s = self.succ(self.blocks if edges is None else edges)
        seen, q = set(), deque([tid])
        while q:
            x = q.popleft()
            for y in s.get(x, []):
                if y not in seen:
                    seen.add(y)
                    q.append(y)
        return seen

    # ---- invariants -------------------------------------------------------
    def find_cycle(self, edges: set[tuple[str, str]]) -> list[str] | None:
        s = self.succ(edges)
        color: dict[str, int] = {}
        stack: list[str] = []

        def dfs(u: str) -> list[str] | None:
            color[u] = 1
            stack.append(u)
            for v in s.get(u, []):
                c = color.get(v, 0)
                if c == 1:
                    return stack[stack.index(v):] + [v]
                if c == 0:
                    r = dfs(v)
                    if r:
                        return r
            stack.pop()
            color[u] = 2
            return None

        for n in list(s):
            if color.get(n, 0) == 0:
                r = dfs(n)
                if r:
                    return r
        return None

    def depth(self, tid: str) -> int | None:
        """Generations from a root along origin; None if the chain doesn't terminate or is broken."""
        seen, d, cur = set(), 0, tid
        while True:
            if cur in seen:
                return None
            seen.add(cur)
            t = self.tasks.get(cur)
            if t is None:
                return None
            o = t.rec.get("origin")
            if not o or o not in self.tasks:
                return d
            d += 1
            cur = o

    def check_invariants(self) -> tuple[list[str], list[str]]:
        errors, warnings = [], []
        for name in ("blocks", "supersedes"):
            c = self.find_cycle(getattr(self, name))
            if c:
                errors.append(f"{name} has a cycle: {' -> '.join(c)}")
        for t in self.tasks.values():
            for a in t.rec.get("blocked_by") or []:
                if a not in self.tasks:
                    errors.append(f"{t.id}: blocked_by references unknown task {a}")
            for rel in t.rec.get("related") or []:
                if rel.get("task") not in self.tasks:
                    warnings.append(f"{t.id}: related references unknown task {rel.get('task')}")
            if self.depth(t.id) is None:
                errors.append(f"{t.id}: origin chain does not terminate")
            if t.is_open and t.rec.get("kind") != "milestone" and t.rec.get("queue") == "scribers":
                if not self.reaches_open_milestone(t.id):
                    warnings.append(f"{t.id}: orphan (no blocks-path to an open milestone)")
            # justified blocks edges: overlap in scope decls, or a reason recorded on the edge
            for a in t.rec.get("blocked_by") or []:
                if t.rec.get("kind") == "milestone":
                    break  # a milestone's blockers are its definition; no justification needed
                if a in self.tasks and not _decl_overlap(self.tasks[a], t) and not _edge_reason(t, a):
                    warnings.append(f"{a} -> {t.id}: blocks edge without shared declaration or reason")
        return errors, warnings

    def reaches_open_milestone(self, tid: str) -> bool:
        return any(self.tasks[d].rec.get("kind") == "milestone" and self.tasks[d].is_open
                   for d in self.descendants(tid) if d in self.tasks)

    # ---- analyses ---------------------------------------------------------
    def milestone_weight(self, mid: str) -> float:
        w = float((self.tasks[mid].rec.get("priority") or {}).get("omri_weight", 0))
        return max(0.25, 1.0 + 0.5 * w)

    def unlock_value(self, tid: str) -> float:
        """Open descendants, each weighted by the milestones it leads to."""
        total = 0.0
        for d in self.descendants(tid):
            t = self.tasks.get(d)
            if not t or not t.is_open:
                continue
            ms = [m for m in self.descendants(d) | {d} if self.tasks[m].rec.get("kind") == "milestone" and self.tasks[m].is_open]
            total += max([self.milestone_weight(m) for m in ms], default=0.5)
        return total

    def node_cost(self, tid: str) -> float:
        t = self.tasks[tid]
        if not t.is_open:
            return 0.0
        return 1.0 + float((t.rec.get("priority") or {}).get("risk", 0.0))

    def longest_paths(self) -> dict[str, float]:
        """Longest path (in expected attempts) from each node to any sink, over open nodes."""
        s = self.succ(self.blocks)
        memo: dict[str, float] = {}

        active: set[str] = set()

        def lp(u: str) -> float:
            if u in memo:
                return memo[u]
            if u in active:          # cycle: invariant violation is reported elsewhere; don't recurse forever
                return 0.0
            active.add(u)
            best = 0.0
            for v in s.get(u, []):
                if v in self.tasks:
                    best = max(best, lp(v))
            active.discard(u)
            memo[u] = self.node_cost(u) + best
            return memo[u]

        for n in self.tasks:
            lp(n)
        return memo

    def longest_into(self) -> dict[str, float]:
        """Longest path (in expected attempts) ending at each node, over open nodes."""
        p = self.pred(self.blocks)
        memo: dict[str, float] = {}

        active: set[str] = set()

        def lin(u: str) -> float:
            if u in memo:
                return memo[u]
            if u in active:
                return 0.0
            active.add(u)
            best = 0.0
            for v in p.get(u, []):
                if v in self.tasks:
                    best = max(best, lin(v))
            active.discard(u)
            memo[u] = best + self.node_cost(u)
            return memo[u]

        for n in self.tasks:
            lin(n)
        return memo

    def critical_path_share(self, tid: str, lps: dict[str, float] | None = None, lins: dict[str, float] | None = None) -> float:
        """Length of the longest chain through tid to a milestone / longest chain into that milestone."""
        lps = lps or self.longest_paths()
        lins = lins or self.longest_into()
        through = lins.get(tid, 0.0) + lps.get(tid, 0.0) - self.node_cost(tid)
        ms = [m for m in self.descendants(tid) if self.tasks[m].rec.get("kind") == "milestone" and self.tasks[m].is_open]
        if not ms:
            return 0.0
        longest_into_ms = max(lins.get(m, 0.0) for m in ms)
        return through / longest_into_ms if longest_into_ms > 0 else 0.0

    def blocking_breadth(self, tid: str) -> int:
        """Tasks that would be ready if tid were done and nothing else changed."""
        n = 0
        for b in self.succ(self.blocks).get(tid, []):
            t = self.tasks.get(b)
            if not t or not t.is_open or t.status in {"dispatched", "in_progress", "checkpoint", "submitted"}:
                continue
            others = [a for a in t.rec.get("blocked_by") or [] if a != tid]
            if all(self.tasks[a].is_done for a in others if a in self.tasks):
                n += 1
        return n

    def ready_now(self, tid: str) -> bool:
        t = self.tasks[tid]
        return all(self.tasks[a].is_done for a in t.rec.get("blocked_by") or [] if a in self.tasks)

    def hubs(self, min_in: int = 3) -> list[tuple[str, int]]:
        p = self.pred(self.blocks)
        s = self.succ(self.blocks)
        out = [(n, len(s.get(n, []))) for n in self.tasks
               if self.tasks[n].is_open and self.tasks[n].rec.get("kind") != "milestone" and len(s.get(n, [])) >= min_in]
        return sorted(out, key=lambda x: -x[1])

    def chains(self, min_len: int = 3) -> list[list[str]]:
        """Maximal paths where every interior node has in- and out-degree 1 (split candidates)."""
        s, p = self.succ(self.blocks), self.pred(self.blocks)
        seen, out = set(), []
        for n in self.tasks:
            if n in seen or len(p.get(n, [])) == 1:
                continue
            cur, chain = n, [n]
            while len(s.get(cur, [])) == 1 and len(p.get(s[cur][0], [])) == 1:
                cur = s[cur][0]
                chain.append(cur)
            seen.update(chain)
            if len(chain) >= min_len:
                out.append(chain)
        return out

    def unlock_yield(self) -> dict[str, dict[str, int]]:
        """Per root of the unlocks forest: proposed descendants, and how many merged."""
        s = self.succ(self.unlocks)
        roots = [t.id for t in self.tasks.values() if not (t.rec.get("origin") in self.tasks)]
        out = {}
        for r in roots:
            ds = self.descendants(r, self.unlocks)
            out[r] = {"descendants": len(ds), "merged": sum(1 for d in ds if self.tasks[d].status == "merged"),
                      "open": sum(1 for d in ds if self.tasks[d].is_open)}
        return out

    def stale_on_statement_change(self, tid: str, decls: list[str]) -> list[str]:
        """Descendants whose scope names any of the changed declarations."""
        out = []
        for d in self.descendants(tid):
            t = self.tasks[d]
            sd = set(((t.rec.get("scope") or {}).get("decls")) or [])
            if t.is_open and sd & set(decls):
                out.append(d)
        return sorted(out)

    def independent_set(self, ranked: list[str], cap: int) -> list[str]:
        """Greedy maximal conflict-free prefix of a ranked list, up to cap."""
        chosen: list[str] = []
        for t in ranked:
            if len(chosen) >= cap:
                break
            if all(tuple(sorted((t, c))) not in self.conflicts for c in chosen):
                chosen.append(t)
        return chosen


# ---- helpers ----------------------------------------------------------------
def _scope(t: Task) -> tuple[set[str], set[str]]:
    sc = t.rec.get("scope") or {}
    return set(sc.get("files") or []), set(sc.get("decls") or [])


def _scope_overlap(a: Task, b: Task) -> bool:
    fa, da = _scope(a)
    fb, db = _scope(b)
    return bool(fa & fb) or bool(da & db)


def _decl_overlap(a: Task, b: Task) -> bool:
    return bool(_scope(a)[1] & _scope(b)[1]) or bool(_scope(a)[0] & _scope(b)[0])


def _edge_reason(t: Task, blocker: str) -> str | None:
    reasons = t.rec.get("blocked_by_reasons") or {}
    return reasons.get(blocker)
