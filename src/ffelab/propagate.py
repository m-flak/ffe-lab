"""Propagate labels upward from what a function calls.

Unknown high-level code calls known low-level code. If a mystery function
calls DrawTriangle and Tex64Trans, it renders *something*. Each callee
contributes a vote for its label, weighted by how trustworthy that label is:

    experiment-backed belief (>= 0.8, >= 2 rows)   weight 1.0
    overall belief >= 0.9 (calls included)          weight 0.7
    inherited hacker name family (unverified)       weight 0.4

Votes become a mild likelihood (at most 3x) stored as ``static_callee``
evidence. It shapes beliefs and experiment choice but never settles a claim,
and re-running replaces the previous propagation instead of stacking it.
Only non-static evidence is used as a source, so propagation can't echo
itself up the call graph.
"""
from __future__ import annotations

import json
from collections import defaultdict

from .ledger import FLOOR, OPINION_KINDS, STATIC_KINDS, Ledger, _normalise

# inherited name family -> top-level label (unverified; weak weight)
FAMILY_LABEL = {
    "Draw": "render", "Blit": "render", "Tex": "render", "Video": "platform", "DPal": "render",
    "Text": "text", "String": "text", "StringExpand": "text", "StringConvert": "text",
    "StringDraw": "text", "FF30": "text", "FF80": "text",
    "Mat": "math", "Vec64": "math", "Int64": "math",
    "Sound": "audio", "Keyb": "io", "Mouse": "io", "Timer": "platform",
    "Gui": "ui", "SysMenu": "ui", "Console": "ui", "CdrFile": "io", "File": "io",
    "ScrnShot": "io", "Scrnshot": "io",
}
W_EXPERIMENT, W_BELIEF, W_NAME = 1.0, 0.7, 0.4
MAX_FACTOR = 3.0


def _non_static_belief(ledger: Ledger, func: int) -> tuple[dict[str, float], int, int]:
    dist = {lab: 1.0 for lab in ledger.labels}
    n_exp = n_all = 0
    for r in ledger.con.execute("SELECT kind, likelihoods FROM evidence WHERE func=?", (func,)):
        if r["kind"] in STATIC_KINDS:
            continue
        like = json.loads(r["likelihoods"])
        for lab in ledger.labels:
            dist[lab] *= max(like.get(lab, 1.0), FLOOR)
        n_all += 1
        n_exp += r["kind"] not in OPINION_KINDS
    return _normalise(dist), n_exp, n_all


def callee_vote(ledger: Ledger, callee: int) -> tuple[str, float, str] | None:
    exp, n = ledger.experimental_belief(callee)
    if n >= 2:
        lab, p = max(exp.items(), key=lambda kv: kv[1])
        if p >= 0.8 and lab != "unknown":
            return lab, W_EXPERIMENT, "experiment"
    bel, _, n_all = _non_static_belief(ledger, callee)
    if n_all:
        lab, p = max(bel.items(), key=lambda kv: kv[1])
        if p >= 0.9 and lab != "unknown":
            return lab, W_BELIEF, "belief"
    row = ledger.con.execute("SELECT prefix FROM functions WHERE num=?", (callee,)).fetchone()
    if row and row["prefix"] in FAMILY_LABEL:
        return FAMILY_LABEL[row["prefix"]], W_NAME, f"name:{row['prefix']}"
    return None


def propagate_one(ledger: Ledger, func: int) -> dict | None:
    callees = sorted({r["callee"] for r in ledger.con.execute(
        "SELECT callee FROM calls WHERE caller=? AND callee<>?", (func, func))})
    votes: dict[str, float] = defaultdict(float)
    why = []
    for c in callees:
        v = callee_vote(ledger, c)
        if v:
            lab, w, src = v
            votes[lab] += w
            why.append((c, lab, src))
    if not votes:
        ledger.con.execute("DELETE FROM evidence WHERE func=? AND kind='static_callee'", (func,))
        return None
    total = sum(votes.values())
    coverage = min(total, 3.0) / 3.0                 # few weak votes -> weak evidence
    like = {lab: 1.0 + (MAX_FACTOR - 1.0) * coverage * (votes.get(lab, 0.0) / total)
            for lab in ledger.labels}
    ledger.replace_evidence(func, "static_callee", "static", like,
                            json.dumps({"callees": why}))
    return {"func": func, "votes": dict(votes), "callees_used": len(why)}


def propagate_all(ledger: Ledger, funcs: list[int] | None = None) -> dict:
    funcs = funcs or [r["caller"] for r in ledger.con.execute("SELECT DISTINCT caller FROM calls")]
    touched = [r for f in funcs if (r := propagate_one(ledger, f))]
    ledger.con.commit()
    return {"functions_updated": len(touched),
            "by_label": _count(touched)}


def _count(rows):
    c = defaultdict(int)
    for r in rows:
        c[max(r["votes"], key=r["votes"].get)] += 1
    return dict(sorted(c.items(), key=lambda kv: -kv[1]))
