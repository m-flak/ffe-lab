"""ffelab command line.

  ffelab build  --src PATH_TO_FFED3D [--db ffe.db] [--out data]
      parse ffe.asm + ffedat.asm, find module tables, segment into files,
      write the SQLite store, data/module_map.json and docs/MODULE_MAP.md
  ffelab uncertain [--db ffe.db] [-n 20]
      most uncertain functions (where the agent should look next)
  ffelab exe-check PATH_TO_FIRSTENC.EXE [--db ffe.db] [--map]
      is this exe the build the asm was disassembled from? stores a per-binary
      address map (identity if same build, aligned by content otherwise)
  ffelab export --src PATH_TO_FFED3D [--db ffe.db] [--out export] [--min-prob 0.9] [--auto]
      write confirmed names into a copy of ffe.asm as alias labels, plus a diff
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import db as dbmod
from .asm import parse_asm
from .ledger import Ledger
from .segment import family_integrity, segment, segments_table
from .tables import find_tables


def build(src: Path, db_path: Path, out: Path, docs: Path) -> dict:
    funcs = parse_asm(src / "ffe.asm")
    tables = find_tables(src / "ffedat.asm")
    con = dbmod.connect(db_path)
    dbmod.ingest(con, funcs, tables)
    seg = segment(funcs, tables)
    segs = segments_table(seg)
    cuts = [0] + seg.boundaries + [len(seg.ordered)]
    con.execute("DELETE FROM segments")
    con.executemany("INSERT INTO segments VALUES (?,?,?,?,?,?,?,?)", [
        (s["seg"], a, z - 1, s["first"], s["last"], s["label"], json.dumps(s["anchors"]), s["boundary_conf"])
        for s, a, z in zip(segs, cuts, cuts[1:])])
    con.commit()
    out.mkdir(parents=True, exist_ok=True); docs.mkdir(parents=True, exist_ok=True)
    result = {"report": seg.report, "segments": segs, "family_integrity": family_integrity(seg),
              "event_tables": [{"label": t.label, "comment": t.comment, "slots": t.slots}
                               for t in tables if t.kind == "event_table"]}
    (out / "module_map.json").write_text(json.dumps(result, indent=2))
    (docs / "MODULE_MAP.md").write_text(render_markdown(result))
    return result


def render_markdown(r: dict) -> str:
    rep = r["report"]
    lines = [
        "# FFE module map (generated)",
        "",
        "Recovered source-file segments of `ffe.asm`, in original link order. "
        "Regenerate with `ffelab build`. Boundary confidence is the model's probability "
        "that a file boundary sits immediately before the segment.",
        "",
        "## Model quality",
        "",
        f"- Functions in address order: {rep['functions']}",
        f"- Module event tables used as anchors: {rep['event_tables']}",
        f"- Labelled name pairs: {rep['labelled_pairs']} ({rep['positive_pairs']} straddle a family change)",
        f"- Cross-validated AUC on held-out name pairs: **{rep['model_cv_auc']}**",
        f"- In-sample pair fit at threshold {rep['threshold']}: {rep['pair_fit_in_sample']}",
        f"- Boundaries chosen: {rep['boundaries']}",
        "",
        "Per-feature AUC (how well each signal alone separates boundaries):",
        "",
        "| feature | AUC | weight |", "|---|---|---|",
    ]
    for k, v in rep["feature_auc"].items():
        lines.append(f"| {k} | {v} | {rep['weights'][k]} |")
    lines += ["", "## Event tables (module anchors)", "", "| table | comment | slots (event → FUNC) |", "|---|---|---|"]
    for t in r["event_tables"]:
        slots = ", ".join(f"{i}→{s}" for i, s in enumerate(t["slots"]) if s is not None)
        lines.append(f"| {t['label']} | {t['comment'] or '—'} | {slots} |")
    lines += ["", "## Segments", "",
              "| # | FUNC range | Pos range | size | boundary conf | label | named examples |",
              "|---|---|---|---|---|---|---|"]
    for s in r["segments"]:
        ex = ", ".join(s["named_examples"][:4]) or "—"
        lines.append(f"| {s['seg']} | {s['first']}–{s['last']} | {s['pos_first']}–{s['pos_last']} | "
                     f"{s['size']} | {s['boundary_conf'] if s['boundary_conf'] is not None else '—'} | "
                     f"{s['label'] or '?'} | {ex} |")
    lines += ["", "## Named-block integrity", "",
              "Contiguous runs of same-family names and how many chosen boundaries fall inside them "
              "(ideally 0; some families, e.g. Gui/Keyb/Mouse, may genuinely share or split files).",
              "", "| family | named | span | boundaries inside |", "|---|---|---|---|"]
    for b in r["family_integrity"]:
        lines.append(f"| {b['family']} | {b['named']} | {b['span']} | {b['boundaries_inside']} |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ffelab")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--src", required=True, type=Path)
    b.add_argument("--db", default="ffe.db", type=Path); b.add_argument("--out", default="data", type=Path)
    b.add_argument("--docs", default="docs", type=Path)
    u = sub.add_parser("uncertain"); u.add_argument("--db", default="ffe.db", type=Path)
    u.add_argument("-n", type=int, default=20)
    e = sub.add_parser("exe-check"); e.add_argument("exe", type=Path)
    e.add_argument("--db", default="ffe.db", type=Path)
    e.add_argument("--map", action="store_true", help="align by content even if it's the same build")
    x = sub.add_parser("export"); x.add_argument("--src", required=True, type=Path)
    x.add_argument("--db", default="ffe.db", type=Path); x.add_argument("--out", default="export", type=Path)
    x.add_argument("--min-prob", type=float, default=0.9)
    x.add_argument("--auto", action="store_true",
                   help="also name functions from their deepest experiment-backed label path")
    a = ap.parse_args(argv)
    if a.cmd == "build":
        r = build(a.src, a.db, a.out, a.docs)
        print(json.dumps(r["report"], indent=2))
    elif a.cmd == "exe-check":
        from . import exe
        r = exe.run(dbmod.connect(a.db), a.exe, force_map=a.map)
        print(json.dumps(r, indent=2))
        print(f"\nUse it with the emulator: FFELAB_EMU='{{\"binary\": \"{r['binary_id']}\", ...}}'")
    elif a.cmd == "export":
        from . import export
        r = export.export(dbmod.connect(a.db), a.src, a.out, a.min_prob, a.auto)
        print(json.dumps({k: v for k, v in r.items() if k != "names"}, indent=2))
    elif a.cmd == "uncertain":
        led = Ledger(dbmod.connect(a.db))
        for f, h in led.most_uncertain(a.n):
            print(f"FUNC_{f:06d}  entropy={h:.2f} bits")


if __name__ == "__main__":
    main()
