"""Write confirmed names back into the asm, keeping it buildable.

Names go into a *copy* of ffe.asm as alias labels, following the convention
the file already uses:

    FUNC_000688:
    _FUNC_000688_SpawnTraders:   ; ffelab: ai/traffic/spawning p=0.94 ev=12,15

Adding an alias instead of renaming keeps every existing reference working,
including the C++ side of FFED3D. Functions that already carry an inherited
name get the evidence as a comment on their label instead of a second name.

What gets exported:
  * names committed with ``Ledger.commit_name`` (belief >= --min-prob)
  * with ``--auto``: functions whose deepest *experiment-backed* label path
    agrees with a belief >= --min-prob
Never exported: functions with an open contradiction.

Output: <out>/ffe.asm, <out>/ffe.asm.diff (review this), <out>/export_report.json
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
from pathlib import Path

from . import labels as lbl
from .asm import LABEL_RE
from .ledger import Ledger


def camel(path: str) -> str:
    """'render/planet/atmosphere/clouds' -> 'RenderPlanetAtmosphereClouds'."""
    parts = re.split(r"[/_\W]+", path)
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


def plan(con: sqlite3.Connection, min_prob: float = 0.9, auto: bool = False) -> dict[int, dict]:
    led = Ledger(con)
    blocked = {r["func"] for r in con.execute("SELECT DISTINCT func FROM contradictions WHERE status='open'")}
    out: dict[int, dict] = {}
    for r in con.execute("SELECT * FROM names"):
        f = r["func"]
        if f in blocked or (r["prob"] or 0) < min_prob:
            continue
        ev = json.loads(r["provenance"] or "{}").get("evidence", [])
        out[f] = {"name": camel(r["name"]) if "/" in r["name"] else r["name"],
                  "path": r["name"] if "/" in r["name"] else None, "p": r["prob"],
                  "evidence": ev[-6:], "source": "committed"}
    if auto:
        funcs = {r["func"] for r in con.execute("SELECT DISTINCT func FROM func_labels WHERE experiment_id IS NOT NULL")}
        for f in funcs - set(out) - blocked:
            d = lbl.deepest_label(con, f)
            if not d or d["experiment_id"] is None:
                continue
            b = led.belief(f)
            top, p = max(b.items(), key=lambda kv: kv[1])
            if top != lbl.top(d["path"]) or p < min_prob:
                continue
            ev = [x["id"] for x in con.execute(
                "SELECT id FROM evidence WHERE func=? AND experiment_id IS NOT NULL", (f,))]
            out[f] = {"name": camel(d["path"]), "path": d["path"], "p": p,
                      "evidence": ev[-6:], "source": "auto"}
    return out


def _comment(info: dict) -> str:
    what = info["path"] or info["name"]
    ev = ",".join(map(str, info["evidence"])) or "-"
    return f"ffelab: {what} p={info['p']:.2f} ev={ev}"


def apply(asm_text: str, names: dict[int, dict], inherited: dict[int, str]) -> tuple[str, list[dict]]:
    lines = asm_text.splitlines(keepends=True)
    existing = {m.group(1) for line in lines if (m := LABEL_RE.match(line))}
    seen: set[int] = set()
    out, done = [], []
    for line in lines:
        m = LABEL_RE.match(line)
        if not m:
            out.append(line)
            continue
        num = int(m.group(2))
        info = names.get(num)
        first = num not in seen
        seen.add(num)
        if not info:
            out.append(line)
            continue
        if inherited.get(num):
            # annotate the inherited-name label once; no second name
            if m.group(3) and "ffelab:" not in line:
                out.append(line.rstrip("\r\n") + f"  ; {_comment(info)}\n")
                done.append({"func": num, "action": "annotated", "kept_name": inherited[num], **info})
            else:
                out.append(line)
            continue
        out.append(line)
        if first:
            label = f"_FUNC_{num:06d}_{info['name']}"
            if label in existing:
                continue
            out.append(f"{label}:\t\t; {_comment(info)}\n")
            existing.add(label)
            done.append({"func": num, "action": "alias", "label": label, **info})
    return "".join(out), done


def export(con: sqlite3.Connection, src: str | Path, out_dir: str | Path,
           min_prob: float = 0.9, auto: bool = False) -> dict:
    src, out_dir = Path(src), Path(out_dir)
    asm_path = src / "ffe.asm" if src.is_dir() else src
    original = asm_path.read_text(errors="replace")
    names = plan(con, min_prob, auto)
    inherited = {r["num"]: r["name"] for r in con.execute("SELECT num, name FROM functions WHERE name IS NOT NULL")}
    new, done = apply(original, names, inherited)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ffe.asm").write_text(new)
    diff = difflib.unified_diff(original.splitlines(keepends=True), new.splitlines(keepends=True),
                                fromfile="a/ffe.asm", tofile="b/ffe.asm", n=2)
    (out_dir / "ffe.asm.diff").write_text("".join(diff))
    report = {"aliases_added": sum(d["action"] == "alias" for d in done),
              "annotated": sum(d["action"] == "annotated" for d in done),
              "skipped_open_contradictions": con.execute(
                  "SELECT COUNT(DISTINCT func) AS n FROM contradictions WHERE status='open'").fetchone()["n"],
              "min_prob": min_prob, "auto": auto, "out": str(out_dir), "names": done}
    (out_dir / "export_report.json").write_text(json.dumps(report, indent=2))
    return report
