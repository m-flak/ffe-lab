"""Call-graph structure: a learned basis, call layers and the subsystem quotient.

Three static views of the call graph, all built from the ``calls`` table only
(no names, no addresses), and two kinds of label evidence derived from them.
Findings behind the choices are in ``docs/GRAPH.md``.

* **Spectral basis** (embedding model ``callgraph-spectral-v1``). The graph
  Laplacian is a matrix with one row per function; its eigenvectors form a
  basis for "one number per function". The smoothest ones (changing least
  across call edges) place functions that call each other, directly or through
  shared partners, close together. Each function gets 32 of those coordinates,
  stored next to ``asm-ngram-v1`` so ``ffelab similar`` and pgvector search them.
* **Call layers** (table ``graph_features``). Cycles are collapsed to one node
  each, which leaves a DAG; ``height`` is the longest call chain below a function
  (0 = calls nothing), ``depth`` the longest chain above it.
* **Quotient.** Collapse every function with a known label to its label and you
  get a small label-to-label call graph. Calls mostly stay inside a label or go
  one way between labels, so a function's labelled callers and callees rule out
  most labels for it.

Evidence (``vote_all``):

* ``static_graph_knn``: the labels of a function's nearest neighbours in the
  spectral basis, weighted by similarity and by how well each label is known.
* ``static_quotient``: labels consistent with the quotient get a mild boost.

Both are static: they shape beliefs and experiment choice, never settle a claim
or raise a contradiction, and re-running replaces them. Known labels come from
``propagate.callee_vote`` (experiments, non-static beliefs, inherited name
families), which ignores static evidence, so the votes can't feed on themselves.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict

import numpy as np

from . import embed
from .ledger import Ledger
from .propagate import callee_vote

MODEL = "callgraph-spectral-v1"
DIM = 32
KNN_K = 10
KNN_MAX_FACTOR = 2.0
QUOTIENT_FACTOR = 2.0
SMOOTH_MAX = 0.5


# ---------------------------------------------------------------- the graph
def _graph(con) -> tuple[list[int], dict[int, int], np.ndarray]:
    """Function numbers, their row index, and W[a, b] = call sites from a to b."""
    nums = [r["num"] for r in con.execute("SELECT num FROM functions ORDER BY num")]
    idx = {f: i for i, f in enumerate(nums)}
    W = np.zeros((len(nums), len(nums)))
    for r in con.execute("SELECT caller, callee FROM calls"):
        a, b = idx.get(r["caller"]), idx.get(r["callee"])
        if a is not None and b is not None and a != b:
            W[a, b] += 1
    return nums, idx, W


def spectral_vectors(con, dim: int = DIM) -> dict[int, np.ndarray]:
    """Unit-length spectral coordinates for every function with at least one call
    in or out. Unweighted and undirected: weighting by call sites or encoding
    direction scored no better on the real graph (docs/GRAPH.md)."""
    nums, _, W = _graph(con)
    if not nums:
        return {}
    A = ((W + W.T) > 0).astype(float)
    deg = A.sum(1)
    dm = 1.0 / np.sqrt(deg + 1e-3)                     # a small ridge keeps isolated functions finite
    L = np.eye(len(nums)) - dm[:, None] * A * dm[None, :]
    vals, vecs = np.linalg.eigh(L)
    # drop each connected piece's constant vector, and keep only smooth directions: on the
    # real graph the first 32 all have eigenvalue < 0.15, so the 0.5 cap only bites on tiny graphs
    E = (dm[:, None] * vecs)[:, (vals > 1e-2) & (vals < SMOOTH_MAX)][:, :dim]
    if E.shape[1] < dim:                                # tiny graphs: pad so every vector has one length
        E = np.hstack([E, np.zeros((len(nums), dim - E.shape[1]))])
    conn = deg > 0
    if not conn.any():
        return {}
    Z = (E - E[conn].mean(0)) / (E[conn].std(0) + 1e-12)
    out = {}
    for i in np.nonzero(conn)[0]:
        n = float(np.linalg.norm(Z[i]))
        if n > 0:
            out[nums[i]] = (Z[i] / n).astype(np.float32)
    return out


def _sccs(succ: list[list[int]]) -> list[list[int]]:
    """Strongly connected components (Tarjan, iterative), callees before callers."""
    n = len(succ)
    index: list[int | None] = [None] * n
    low = [0] * n
    on = [False] * n
    stack: list[int] = []
    out: list[list[int]] = []
    counter = 0
    for root in range(n):
        if index[root] is not None:
            continue
        work = [(root, 0)]
        while work:
            v, i = work[-1]
            if i == 0 and index[v] is None:
                index[v] = low[v] = counter
                counter += 1
                stack.append(v)
                on[v] = True
            descended = False
            while i < len(succ[v]):
                w = succ[v][i]
                i += 1
                if index[w] is None:
                    work[-1] = (v, i)
                    work.append((w, 0))
                    descended = True
                    break
                if on[w]:
                    low[v] = min(low[v], index[w])
            if descended:
                continue
            work.pop()
            if work:
                u = work[-1][0]
                low[u] = min(low[u], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on[w] = False
                    comp.append(w)
                    if w == v:
                        break
                out.append(comp)
    return out


def features(con) -> list[dict]:
    """Per function: fan-in, fan-out, median link-order distance to its call
    partners, call height and depth, and the size of the cycle it sits in."""
    nums, idx, W = _graph(con)
    if not nums:
        return []
    ords = {r["num"]: r["ord"] for r in con.execute("SELECT num, ord FROM functions")}
    succ = [np.nonzero(W[i])[0].tolist() for i in range(len(nums))]
    pred = [np.nonzero(W[:, i])[0].tolist() for i in range(len(nums))]
    comps = _sccs(succ)
    comp_of = {v: c for c, vs in enumerate(comps) for v in vs}
    height = [0] * len(comps)
    for c, vs in enumerate(comps):                      # callees come first
        height[c] = max((height[comp_of[w]] + 1 for v in vs for w in succ[v] if comp_of[w] != c), default=0)
    depth = [0] * len(comps)
    for c in reversed(range(len(comps))):               # callers first
        for v in comps[c]:
            for w in succ[v]:
                if comp_of[w] != c:
                    depth[comp_of[w]] = max(depth[comp_of[w]], depth[c] + 1)
    rows = []
    for i, f in enumerate(nums):
        partners = set(succ[i]) | set(pred[i])
        dists = [abs(ords[f] - ords[nums[j]]) for j in partners
                 if ords[f] is not None and ords[nums[j]] is not None]
        c = comp_of[i]
        rows.append({"func": f, "fan_in": len(pred[i]), "fan_out": len(succ[i]),
                     "span": float(np.median(dists)) if dists else None,
                     "height": height[c], "depth": depth[c],
                     "cycle": c if len(comps[c]) > 1 else None, "cycle_size": len(comps[c])})
    return rows


def build(con) -> dict:
    """Recompute ``graph_features`` and the ``callgraph-spectral-v1`` embeddings."""
    rows = features(con)
    con.execute("DELETE FROM graph_features")
    con.executemany(
        "INSERT INTO graph_features (func, fan_in, fan_out, span, height, depth, cycle, cycle_size)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [(r["func"], r["fan_in"], r["fan_out"], r["span"], r["height"], r["depth"], r["cycle"], r["cycle_size"])
         for r in rows])
    con.commit()
    n_vec = embed.build(con, MODEL)
    cycles = Counter(r["cycle"] for r in rows if r["cycle"] is not None)
    return {"functions": len(rows), "embedded": n_vec,
            "max_height": max((r["height"] for r in rows), default=0),
            "cycles": len(cycles), "largest_cycle": max(cycles.values(), default=0)}


# ---------------------------------------------------------------- label votes
def known_labels(ledger: Ledger) -> dict[int, tuple[str, float]]:
    """func -> (label, weight) for every function whose label is known well enough
    to vote: experiment-backed, a confident non-static belief, or an inherited name
    family (weakly). Static evidence never counts, so votes can't echo."""
    out = {}
    for r in ledger.con.execute("SELECT num FROM functions"):
        v = callee_vote(ledger, r["num"])
        if v:
            out[r["num"]] = (v[0], v[1])
    return out


def _edges(con) -> list[tuple[int, int]]:
    return sorted({(r["caller"], r["callee"]) for r in con.execute("SELECT caller, callee FROM calls")
                   if r["caller"] != r["callee"]})


def quotient(edges, labels: dict[int, tuple[str, float]], skip: int | None = None) -> Counter:
    """Label-to-label call counts between functions with known labels (optionally
    leaving out one function's edges, to test it against the rest)."""
    q = Counter()
    for a, b in edges:
        if skip in (a, b) or a not in labels or b not in labels:
            continue
        q[(labels[a][0], labels[b][0])] += 1
    return q


def allowed_labels(func: int, edges, labels, q: Counter, all_labels) -> set[str] | None:
    """Labels ``func`` could have without adding a label-to-label call the quotient
    has never seen. None when it has no labelled neighbours."""
    outs = {labels[b][0] for a, b in edges if a == func and b in labels}
    ins = {labels[a][0] for a, b in edges if b == func and a in labels}
    if not outs and not ins:
        return None
    return {lab for lab in all_labels
            if all(lab == m or q[(lab, m)] for m in outs) and all(lab == k or q[(k, lab)] for k in ins)}


def _matrix(con) -> tuple[list[int], np.ndarray]:
    rows = con.execute("SELECT func, vec FROM embeddings WHERE model=?", (MODEL,)).fetchall()
    if not rows:
        return [], np.zeros((0, DIM), np.float32)
    return [r["func"] for r in rows], np.stack([embed._decode(r["vec"]) for r in rows])


def knn_votes(func: int, funcs: list[int], M: np.ndarray, pos: dict[int, int],
              labels, k: int = KNN_K) -> dict[str, float]:
    if func not in pos:
        return {}
    s = M @ M[pos[func]]
    s[pos[func]] = -np.inf
    votes: dict[str, float] = defaultdict(float)
    for j in np.argsort(-s)[:k]:
        g = funcs[j]
        if g in labels and s[j] > 0:
            votes[labels[g][0]] += float(s[j]) * labels[g][1]
    return dict(votes)


def vote_all(ledger: Ledger, k: int = KNN_K) -> dict:
    """Write ``static_graph_knn`` and ``static_quotient`` evidence for every
    function (replacing earlier runs). Needs ``build`` first."""
    con = ledger.con
    labels = known_labels(ledger)
    edges = _edges(con)
    q_all = quotient(edges, labels)
    funcs, M = _matrix(con)
    pos = {f: i for i, f in enumerate(funcs)}
    stats = Counter()
    for r in con.execute("SELECT num FROM functions").fetchall():
        f = r["num"]
        votes = knn_votes(f, funcs, M, pos, labels, k)
        if votes:
            total = sum(votes.values())
            coverage = min(total, 3.0) / 3.0
            like = {lab: 1.0 + (KNN_MAX_FACTOR - 1.0) * coverage * votes.get(lab, 0.0) / total
                    for lab in ledger.labels}
            ledger.replace_evidence(f, "static_graph_knn", "static", like,
                                    json.dumps({"votes": {a: round(b, 3) for a, b in votes.items()}}))
            stats["knn"] += 1
        else:
            con.execute("DELETE FROM evidence WHERE func=? AND kind='static_graph_knn'", (f,))
        q = quotient(edges, labels, skip=f) if f in labels else q_all
        allowed = allowed_labels(f, edges, labels, q, ledger.labels)
        if allowed:
            like = {lab: QUOTIENT_FACTOR if lab in allowed else 1.0 for lab in ledger.labels}
            ledger.replace_evidence(f, "static_quotient", "static", like,
                                    json.dumps({"allowed": sorted(allowed)}))
            stats["quotient"] += 1
        else:
            con.execute("DELETE FROM evidence WHERE func=? AND kind='static_quotient'", (f,))
    con.commit()
    return {"knn_votes": stats["knn"], "quotient_votes": stats["quotient"], "labelled": len(labels)}


def outliers(ledger: Ledger) -> list[dict]:
    """Calls between labelled functions that no other labelled call supports: each
    is a mislabel or a real cross-layer call. For review (red team, contradictions),
    never an automatic verdict."""
    labels = known_labels(ledger)
    edges = _edges(ledger.con)
    out = []
    for a, b in edges:
        if a not in labels or b not in labels or labels[a][0] == labels[b][0]:
            continue
        pair = (labels[a][0], labels[b][0])
        support = sum(1 for x, y in edges if (x, y) != (a, b) and x in labels and y in labels
                      and (labels[x][0], labels[y][0]) == pair)
        if support == 0:
            out.append({"caller": a, "caller_label": pair[0], "callee": b, "callee_label": pair[1]})
    return out


def check(ledger: Ledger, k: int = KNN_K) -> dict:
    """Hold-out test on functions with known labels: hide each one's label and see
    whether the neighbour vote picks it and whether the quotient still allows it."""
    con = ledger.con
    labels = known_labels(ledger)
    edges = _edges(con)
    funcs, M = _matrix(con)
    pos = {f: i for i, f in enumerate(funcs)}
    knn_n = knn_ok = q_n = q_ok = 0
    sizes = []
    for f, (lab, _) in labels.items():
        rest = {g: v for g, v in labels.items() if g != f}
        votes = knn_votes(f, funcs, M, pos, rest, k)
        if votes:
            knn_n += 1
            knn_ok += max(votes, key=votes.get) == lab
        allowed = allowed_labels(f, edges, rest, quotient(edges, rest), ledger.labels)
        if allowed is not None:
            q_n += 1
            q_ok += lab in allowed
            sizes.append(len(allowed))
    chance = Counter(v[0] for v in labels.values())
    total = sum(chance.values())
    return {
        "labelled": len(labels),
        "knn_top_label_accuracy": round(knn_ok / knn_n, 3) if knn_n else None, "knn_tested": knn_n,
        "majority_label_rate": round(max(chance.values()) / total, 3) if total else None,
        "quotient_contains_true_label": round(q_ok / q_n, 3) if q_n else None, "quotient_tested": q_n,
        "quotient_median_allowed": float(np.median(sizes)) if sizes else None,
        "labels": len(ledger.labels),
    }
