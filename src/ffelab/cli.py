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
  ffelab embed [--db ffe.db] [--model asm-ngram-v1|fp-scenario-v1|all]
      compute embeddings for similarity search
  ffelab similar FUNC [--db ffe.db] [--model asm-ngram-v1|callgraph-spectral-v1] [-k 10]
      functions most like FUNC (numbers and scores only; no names or beliefs)
  ffelab graph build|vote|outliers|check [--db ffe.db]
      call-graph structure (docs/GRAPH.md): build = call layers + spectral basis
      (embedding callgraph-spectral-v1); vote = neighbour and quotient label evidence
      (static, never settles anything); outliers = calls between labels nothing else
      supports; check = hold-out accuracy of the votes
  ffelab db copy --from ffe.db --to postgresql:///ffelab
      copy a whole store into an empty one (SQLite -> Postgres, or back)
  ffelab eval export --db ffe.db [--out evals/datasets]
      eval cases from experiment-settled labels (blind labeller and red team)
  ffelab eval run --dataset FILE [--solver claude|heuristic|replay] [--model M]
                  [--prompt AGENT.md] [--effort LEVEL] [--split dev|test] [--out REPORT.json]
      score an agent; prints metrics, writes a report
  ffelab eval gate --report REPORT.json --baseline BASELINE.json
      exit 1 if the run is worse than the baseline
  ffelab eval baseline --report REPORT.json --out BASELINE.json [--margin 0.05]
      turn a measured run into gate thresholds
  ffelab eval compare REPORT.json [REPORT.json ...] [--split test]
      side-by-side table of runs (models, prompts, solvers)

--db takes a SQLite file or a postgresql:// URL, and defaults to $FFELAB_DB, else ffe.db.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from . import db as dbmod
from .asm import parse_asm
from .ledger import Ledger
from .segment import family_integrity, segment, segments_table
from .tables import find_tables


def build(src: Path, db_path: str | Path, out: Path, docs: Path) -> dict:
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


DEFAULT_DB = os.environ.get("FFELAB_DB", "ffe.db")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ffelab")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--src", required=True, type=Path)
    b.add_argument("--db", default=DEFAULT_DB); b.add_argument("--out", default="data", type=Path)
    b.add_argument("--docs", default="docs", type=Path)
    u = sub.add_parser("uncertain"); u.add_argument("--db", default=DEFAULT_DB)
    u.add_argument("-n", type=int, default=20)
    e = sub.add_parser("exe-check"); e.add_argument("exe", type=Path)
    e.add_argument("--db", default=DEFAULT_DB)
    e.add_argument("--map", action="store_true", help="align by content even if it's the same build")
    x = sub.add_parser("export"); x.add_argument("--src", required=True, type=Path)
    x.add_argument("--db", default=DEFAULT_DB); x.add_argument("--out", default="export", type=Path)
    x.add_argument("--min-prob", type=float, default=0.9)
    x.add_argument("--auto", action="store_true",
                   help="also name functions from their deepest experiment-backed label path")
    em = sub.add_parser("embed"); em.add_argument("--db", default=DEFAULT_DB)
    em.add_argument("--model", default="all")
    sm = sub.add_parser("similar"); sm.add_argument("func", type=int); sm.add_argument("--db", default=DEFAULT_DB)
    sm.add_argument("--model", default="asm-ngram-v1"); sm.add_argument("-k", type=int, default=10)
    gr = sub.add_parser("graph"); gr.add_argument("graph_cmd", choices=["build", "vote", "outliers", "check"])
    gr.add_argument("--db", default=DEFAULT_DB); gr.add_argument("-k", type=int, default=10)
    dbc = sub.add_parser("db").add_subparsers(dest="db_cmd", required=True)
    cp = dbc.add_parser("copy"); cp.add_argument("--from", dest="src", required=True)
    cp.add_argument("--to", dest="dst", required=True)
    ev = sub.add_parser("eval").add_subparsers(dest="eval_cmd", required=True)
    ex = ev.add_parser("export"); ex.add_argument("--db", default=DEFAULT_DB)
    ex.add_argument("--out", default="evals/datasets", type=Path)
    ex.add_argument("--threshold", type=float, default=0.95); ex.add_argument("--min-evidence", type=int, default=3)
    er = ev.add_parser("run"); er.add_argument("--dataset", required=True, type=Path)
    er.add_argument("--solver", choices=["claude", "heuristic", "replay"], default="claude")
    er.add_argument("--model", default=None); er.add_argument("--prompt", type=Path, default=None)
    er.add_argument("--effort", default=None, choices=["low", "medium", "high", "xhigh", "max"])
    er.add_argument("--replay", type=Path, help="report to replay answers from (--solver replay)")
    er.add_argument("--split", choices=["dev", "test"], default=None); er.add_argument("--out", type=Path)
    eg = ev.add_parser("gate"); eg.add_argument("--report", required=True, type=Path)
    eg.add_argument("--baseline", required=True, type=Path)
    eb = ev.add_parser("baseline"); eb.add_argument("--report", required=True, type=Path)
    eb.add_argument("--out", required=True, type=Path); eb.add_argument("--margin", type=float, default=0.05)
    eb.add_argument("--split", default="all")
    ec = ev.add_parser("compare"); ec.add_argument("reports", nargs="+", type=Path)
    ec.add_argument("--split", default="all")
    a = ap.parse_args(argv)
    if a.cmd == "eval":
        return eval_cmd(a)
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
    elif a.cmd == "embed":
        from . import embed
        con = dbmod.connect(a.db)
        for model in (embed.MODELS if a.model == "all" else [a.model]):
            print(f"{model}: {embed.build(con, model)} functions")
    elif a.cmd == "similar":
        from . import embed
        for r in embed.similar(dbmod.connect(a.db), a.func, a.model, a.k):
            print(f"FUNC_{r['func']:06d}  {r['score']:.3f}")
    elif a.cmd == "graph":
        from . import graph
        con = dbmod.connect(a.db)
        if a.graph_cmd == "build":
            r = graph.build(con)
        elif a.graph_cmd == "vote":
            r = graph.vote_all(Ledger(con), a.k)
        elif a.graph_cmd == "outliers":
            r = graph.outliers(Ledger(con))
        else:
            r = graph.check(Ledger(con), a.k)
        print(json.dumps(r, indent=2))
    elif a.cmd == "db":
        counts = dbmod.copy(dbmod.connect(a.src), dbmod.connect(a.dst))
        print(json.dumps(counts, indent=2))
        print("embeddings are not copied; run `ffelab embed --db DEST`")
    elif a.cmd == "uncertain":
        led = Ledger(dbmod.connect(a.db))
        for f, h in led.most_uncertain(a.n):
            print(f"FUNC_{f:06d}  entropy={h:.2f} bits")


def eval_cmd(a) -> int:
    from .evals import dataset as ds
    from .evals import runner
    from .evals import solvers as sv
    if a.eval_cmd == "export":
        led = Ledger(dbmod.connect(a.db))
        for name, cases in (("blind.jsonl", ds.export_blind(led, a.threshold, a.min_evidence)),
                            ("redteam.jsonl", ds.export_red_team(led))):
            ds.save(cases, a.out / name)
            n_test = sum(c.split == "test" for c in cases)
            print(f"{a.out / name}: {len(cases)} cases ({n_test} held out as test)")
        return 0
    if a.eval_cmd == "run":
        cases = ds.load(a.dataset)
        task = cases[0].task if cases else "blind_label"
        if a.solver == "claude":
            solver = sv.ClaudeSolver(task, a.model or sv.DEFAULT_MODEL, a.prompt, a.effort)
        elif a.solver == "replay":
            solver = sv.ReplaySolver(a.replay)
        else:
            solver = sv.HeuristicSolver(task)

        def progress(i, n, r):
            mark = "ok " if r.correct else ("ERR" if r.output.error else "no ")
            print(f"  [{i}/{n}] {mark} {r.case_id}", flush=True)
        rep = runner.run(cases, solver, str(a.dataset), a.split, progress)
        for split, m in rep.metrics.items():
            print(f"{split}: " + json.dumps(m.model_dump(exclude={"by_subsystem"})))
        if a.out:
            a.out.parent.mkdir(parents=True, exist_ok=True)
            a.out.write_text(rep.model_dump_json(indent=1))
            print(f"report: {a.out}")
        return 0
    if a.eval_cmd == "gate":
        rep = runner.EvalReport.model_validate_json(a.report.read_text())
        base = runner.Baseline.model_validate_json(a.baseline.read_text())
        fails = runner.gate(rep, base)
        tag = f" ({base.status})" if base.status != "measured" else ""
        for f in fails:
            print(f"FAIL{tag}: {f}")
        if not fails:
            print(f"PASS{tag}: {rep.solver} meets {a.baseline}")
        return 1 if fails else 0
    if a.eval_cmd == "baseline":
        rep = runner.EvalReport.model_validate_json(a.report.read_text())
        a.out.write_text(runner.baseline_from(rep, a.split, a.margin).model_dump_json(indent=1) + "\n")
        print(f"wrote {a.out}")
        return 0
    if a.eval_cmd == "compare":
        reps = [runner.EvalReport.model_validate_json(p.read_text()) for p in a.reports]
        print(runner.compare(reps, a.split))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
