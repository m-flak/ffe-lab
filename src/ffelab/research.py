"""Static research: what a researcher agent (Muse, Claude, you) reads and how it
records what it found. Protocol: ``docs/STATIC_RESEARCH.md``.

Reading is unrestricted: names, data references, tables, beliefs and claims are
all visible (unlike ``blind_view``). Writing is claims only. Every claim needs a
*basis* (the static facts it rests on) and becomes an ordinary open claim bet
under the researcher's source, Brier-scored once experiments settle it. Nothing
here runs, settles or resolves anything.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import labels as lbl
from .ledger import STATIC_KINDS, Ledger
from .models import ClaimInput

DATA_LABEL_RE = re.compile(r"^_?DATA_(\d+)")
FUNC_LABEL_RE = re.compile(r"^_?FUNC_(\d+)")


def func_num(ref: str | int) -> int:
    """``1805``, ``FUNC_001805`` or ``FUNC_001805_Name`` -> 1805."""
    if isinstance(ref, int):
        return ref
    m = FUNC_LABEL_RE.match(ref.strip())
    return int(m.group(1)) if m else int(ref)


def data_num(ref: str | int) -> int:
    """``9252``, ``DATA_009252`` or ``_DATA_009252_Name`` -> 9252."""
    if isinstance(ref, int):
        return ref
    m = DATA_LABEL_RE.match(ref.strip())
    return int(m.group(1)) if m else int(ref)


def _name(con, func: int) -> str:
    r = con.execute("SELECT name FROM functions WHERE num=?", (func,)).fetchone()
    return f"FUNC_{func:06d}" + (f"_{r['name']}" if r and r["name"] else "")


def view(ledger: Ledger, func: int, max_lines: int = 200) -> dict:
    """Everything known about one function, names included."""
    con = ledger.con
    row = con.execute("SELECT * FROM functions WHERE num=?", (func,)).fetchone()
    if row is None:
        raise KeyError(func)
    seg = None
    if row["ord"] is not None:
        s = con.execute("SELECT seg, first_num, last_num, label, anchors FROM segments "
                        "WHERE first_ord<=? AND last_ord>=?", (row["ord"], row["ord"])).fetchone()
        if s:
            seg = {"seg": s["seg"], "funcs": f"{s['first_num']}-{s['last_num']}", "label": s["label"],
                   "anchor_tables": json.loads(s["anchors"] or "[]")}
    tables = [{"table": t["label"], "comment": t["comment"], "slot": i}
              for t in con.execute("SELECT label, comment, slots FROM tables")
              for i, f in enumerate(json.loads(t["slots"] or "[]")) if f == func]
    g = con.execute("SELECT height, depth, cycle_size FROM graph_features WHERE func=?", (func,)).fetchone()
    belief = sorted(ledger.belief(func).items(), key=lambda kv: -kv[1])[:3]
    claims = [dict(r) for r in con.execute(
        "SELECT source, label, prob, outcome FROM bets WHERE func=? AND kind='claim' ORDER BY id", (func,))]
    static = {r["kind"]: r["detail"] for r in con.execute(
        "SELECT kind, detail FROM evidence WHERE func=?", (func,)) if r["kind"] in STATIC_KINDS}
    lines = (row["asm_text"] or "").splitlines()
    return {
        "func": _name(con, func), "pos": hex(row["pos"]) if row["pos"] is not None else None,
        "instructions": row["n_ins"],
        "callers": [_name(con, r["caller"]) for r in con.execute(
            "SELECT DISTINCT caller FROM calls WHERE callee=? ORDER BY caller", (func,))],
        "callees": [_name(con, r["callee"]) for r in con.execute(
            "SELECT DISTINCT callee FROM calls WHERE caller=? ORDER BY callee", (func,))],
        "data_refs": [f"DATA_{r['data']:06d}" for r in con.execute(
            "SELECT DISTINCT data FROM data_refs WHERE func=? ORDER BY data", (func,))],
        "in_tables": tables, "segment": seg,
        "call_layers": {"height": g["height"], "depth": g["depth"], "cycle_size": g["cycle_size"]} if g else None,
        "belief_top3": {k: round(v, 3) for k, v in belief},
        "claims": claims, "static_evidence": static,
        "asm": "\n".join(lines[:max_lines]) + (f"\n... ({len(lines) - max_lines} more lines)"
                                               if len(lines) > max_lines else ""),
    }


def refs(con, data: int) -> list[str]:
    """Functions whose code mentions ``DATA_<data>``."""
    return [_name(con, r["func"]) for r in con.execute(
        "SELECT DISTINCT func FROM data_refs WHERE data=? ORDER BY func", (data,))]


def grep(con, pattern: str, limit: int = 50) -> list[dict]:
    """Functions whose asm (names and comments included) matches ``pattern``."""
    rx = re.compile(pattern)
    out = []
    for r in con.execute("SELECT num, asm_text FROM functions ORDER BY num"):
        hits = [ln.strip() for ln in (r["asm_text"] or "").splitlines() if rx.search(ln)]
        if hits:
            out.append({"func": _name(con, r["num"]), "lines": hits[:3], "matches": len(hits)})
            if len(out) >= limit:
                break
    return out


def claim(ledger: Ledger, func: int, label: str, confidence: float, source: str, basis: str) -> dict:
    """Record a researcher's claim. One open claim per source and function: a second
    one would count the same reasoning twice."""
    if not basis.strip():
        raise ValueError("a claim needs a basis: the static facts it rests on")
    if source.split(":")[0] == "human" or ":" not in source:
        raise ValueError(f"research source must be 'ai:<name>' (e.g. ai:muse), got {source!r}")
    ClaimInput(func=func, label=label, confidence=confidence, source=source)
    path = lbl.validate(label, ledger.labels)
    if ledger.con.execute("SELECT 1 FROM functions WHERE num=?", (func,)).fetchone() is None:
        raise KeyError(f"no FUNC {func}")
    prior = ledger.con.execute("SELECT label FROM bets WHERE source=? AND func=? AND kind='claim' "
                               "AND outcome IS NULL", (source, func)).fetchone()
    if prior:
        raise ValueError(f"{source} already has an open claim on FUNC {func} ({prior['label']}); "
                         "one open claim per source and function, or the same reasoning counts twice")
    post = ledger.call(func, path, confidence, source, detail=f"{path}@{confidence:.2f}: {basis.strip()}")
    return {"func": func, "label": path, "confidence": confidence, "source": source,
            "belief_top3": {k: round(v, 3) for k, v in sorted(post.items(), key=lambda kv: -kv[1])[:3]}}


def import_claims(ledger: Ledger, path: Path, source: str) -> list[dict]:
    """Claims from a JSON Lines file, one ``{"func", "label", "confidence", "basis"}``
    per line. All or nothing: one bad line and none are recorded."""
    rows = []
    for n, line in enumerate(Path(path).read_text().splitlines(), 1):
        if line.strip():
            try:
                d = json.loads(line)
                rows.append((func_num(d["func"]), d["label"], float(d["confidence"]), d.get("basis", "")))
            except (ValueError, KeyError, TypeError) as e:
                raise ValueError(f"{path}:{n}: {e}") from e
    with ledger.atomic():
        return [claim(ledger, f, lab, c, source, b) for f, lab, c, b in rows]


RETAG = [("bets", "source"), ("func_labels", "source"), ("evidence", "source"),
         ("blind_labels", "source"), ("vocabulary", "created_by")]


def retag(con, old: str, new: str) -> dict[str, int]:
    """Move everything recorded under ``old`` to ``new`` (one researcher, one record).
    Refused once any of ``old``'s bets is resolved: scores don't move between sources."""
    if con.execute("SELECT 1 FROM bets WHERE source=? AND outcome IS NOT NULL", (old,)).fetchone():
        raise ValueError(f"{old} has resolved bets; its record stays under its own name")
    counts = {}
    from . import db
    with db.atomic(con):
        for table, col in RETAG:
            counts[table] = con.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (new, old)).rowcount
        con.commit()
    return counts
