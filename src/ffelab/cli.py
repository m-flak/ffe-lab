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
  ffelab emu calibrate --exe FIRSTENC.EXE [--binary ID] [--gdb-port 1234] [--limit-mb 64]
      with FFE running in QEMU: find code_base (where the code sits in guest memory)
  ffelab emu monitor "COMMAND" [--monitor-port 4444]
      run one QEMU monitor command (stop, cont, loadvm NAME, info registers, info snapshots)
  ffelab scenario save NAME --tags a,b [--notes TEXT] [--monitor-port 4444]
      save the running game as QEMU snapshot NAME and register it with its tags
  ffelab scenario tag NAME --tags a,b [--notes TEXT]
      register (or re-tag) a save state without saving
  ffelab scenario list [--monitor-port 4444]
      registered scenarios, and whether QEMU has a snapshot for each
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
  ffelab agents run [--db ffe.db] [--backend fake|qemu|dosbox-x] [--worker claude|heuristic]
                    [--model M] [--effort LEVEL] [--rounds N] [--max-usd X] [--max-minutes M]
                    [--blind N] [--attacks N] [--trace phoenix|langfuse|otlp|console|none] [--out REPORT.json]
      run the orchestrator: blind labeller + red team workers under a budget
  ffelab agents questions [--db ffe.db] [--all]
      the batched questions agents and contradictions have queued for you
  ffelab agents answer ID TEXT [--claim LABEL --confidence P] [--close explained|claim_withdrawn]
  ffelab agents dismiss ID
  ffelab research view FUNC | refs DATA | grep REGEX       (read; names included)
  ffelab research claim FUNC LABEL -c P --basis TEXT [--source ai:muse]
  ffelab research import CLAIMS.jsonl [--source ai:muse]
  ffelab research retag OLD_SOURCE NEW_SOURCE
  ffelab research subsystem SPEC [--min-prob 0.5]          (SPEC: seg:N or a label path)
  ffelab research cut-check FUNC [--min-prob 0.5]          (gut | split | keep | unsure, static)
  ffelab research link FFE ZIG RELATION --note TEXT [--zig-root DIR] [--source ai:muse]
      static research (docs/STATIC_RESEARCH.md): read freely, write claims only;
      --source defaults to $FFELAB_SOURCE
  ffelab bridge list [--ffe REF] [--zig PATH] [--status S]
  ffelab bridge status ID proposed|accepted|in_progress|done|rejected
      FFE <-> Zig engine links (design proposals, never evidence; docs/STATIC_RESEARCH.md)

--db takes a SQLite file or a postgresql:// URL, and defaults to $FFELAB_DB, else ffe.db.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
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
    rf = sub.add_parser("ref").add_subparsers(dest="ref_cmd", required=True)
    ri = rf.add_parser("import", help="match FFE to Frontier: Elite II via glFrontier's fe2.s")
    ri.add_argument("--fe2", required=True, type=Path, help="fe2.s from a glFrontier fork")
    ri.add_argument("--src", required=True, type=Path, help="folder with ffe.asm and ffedat.asm")
    ri.add_argument("--db", default=DEFAULT_DB)
    rs = rf.add_parser("show"); rs.add_argument("func", type=int, nargs="?")
    rs.add_argument("--db", default=DEFAULT_DB)
    emu = sub.add_parser("emu").add_subparsers(dest="emu_cmd", required=True)
    ca = emu.add_parser("calibrate"); ca.add_argument("--exe", required=True, type=Path)
    ca.add_argument("--db", default=DEFAULT_DB); ca.add_argument("--binary", default=None)
    ca.add_argument("--gdb-port", type=int, default=1234); ca.add_argument("--limit-mb", type=int, default=64)
    mo = emu.add_parser("monitor"); mo.add_argument("command"); mo.add_argument("--monitor-port", type=int, default=4444)
    sc = sub.add_parser("scenario").add_subparsers(dest="scenario_cmd", required=True)
    for name in ("save", "tag"):
        p = sc.add_parser(name); p.add_argument("name"); p.add_argument("--db", default=DEFAULT_DB)
        p.add_argument("--tags", required=True, help="comma-separated, e.g. near_planet,atmosphere")
        p.add_argument("--notes", default=""); p.add_argument("--monitor-port", type=int, default=4444)
    sl = sc.add_parser("list"); sl.add_argument("--db", default=DEFAULT_DB)
    sl.add_argument("--monitor-port", type=int, default=4444)
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
    ag = sub.add_parser("agents").add_subparsers(dest="agents_cmd", required=True)
    ar = ag.add_parser("run"); ar.add_argument("--db", default=DEFAULT_DB)
    ar.add_argument("--backend", default="fake", choices=["fake", "qemu", "dosbox-x"])
    ar.add_argument("--worker", default="claude", choices=["claude", "heuristic"])
    ar.add_argument("--model", default=None)
    ar.add_argument("--effort", default=None, choices=["low", "medium", "high", "xhigh", "max"])
    ar.add_argument("--rounds", type=int, default=10); ar.add_argument("--max-usd", type=float, default=5.0)
    ar.add_argument("--max-tokens", type=int, default=None); ar.add_argument("--max-minutes", type=float, default=60)
    ar.add_argument("--worker-max-usd", type=float, default=0.5); ar.add_argument("--worker-max-turns", type=int, default=12)
    ar.add_argument("--max-questions", type=int, default=10)
    ar.add_argument("--blind", type=int, default=4, help="blind labels per round")
    ar.add_argument("--attacks", type=int, default=2, help="red-team targets per round")
    ar.add_argument("--concurrency", type=int, default=4)
    ar.add_argument("--trace", default="none", choices=["phoenix", "langfuse", "otlp", "console", "none"])
    ar.add_argument("--out", type=Path)
    aq = ag.add_parser("questions"); aq.add_argument("--db", default=DEFAULT_DB)
    aq.add_argument("--all", action="store_true", help="include answered and dismissed")
    aa = ag.add_parser("answer"); aa.add_argument("id", type=int); aa.add_argument("text")
    aa.add_argument("--db", default=DEFAULT_DB)
    aa.add_argument("--claim", help="also assert this label for the question's function (an opinion)")
    aa.add_argument("--confidence", type=float)
    aa.add_argument("--close", choices=["explained", "claim_withdrawn"], help="close the contradiction asked about")
    ad = ag.add_parser("dismiss"); ad.add_argument("id", type=int); ad.add_argument("--db", default=DEFAULT_DB)
    rs = sub.add_parser("research").add_subparsers(dest="research_cmd", required=True)
    src_default = os.environ.get("FFELAB_SOURCE")
    rv = rs.add_parser("view"); rv.add_argument("func"); rv.add_argument("--max-lines", type=int, default=200)
    rr = rs.add_parser("refs"); rr.add_argument("data")
    rg = rs.add_parser("grep"); rg.add_argument("pattern"); rg.add_argument("-n", type=int, default=50)
    rc = rs.add_parser("claim"); rc.add_argument("func"); rc.add_argument("label")
    rc.add_argument("-c", "--confidence", type=float, required=True)
    rc.add_argument("--basis", required=True, help="the static facts the claim rests on")
    ri = rs.add_parser("import"); ri.add_argument("file", type=Path)
    for p in (rc, ri):
        p.add_argument("--source", default=src_default, required=src_default is None)
    rt = rs.add_parser("retag"); rt.add_argument("old"); rt.add_argument("new")
    rsub = rs.add_parser("subsystem"); rsub.add_argument("spec"); rsub.add_argument("--min-prob", type=float, default=0.5)
    rcut = rs.add_parser("cut-check"); rcut.add_argument("func"); rcut.add_argument("--min-prob", type=float, default=0.5)
    rl = rs.add_parser("link"); rl.add_argument("ffe"); rl.add_argument("zig"); rl.add_argument("relation")
    rl.add_argument("--note", required=True, help="why these meet, and what has to change")
    rl.add_argument("--zig-root", default=os.environ.get("FFELAB_ZIG_ROOT"))
    rl.add_argument("--source", default=src_default, required=src_default is None)
    for p in (rv, rr, rg, rc, ri, rt, rsub, rcut, rl):
        p.add_argument("--db", default=DEFAULT_DB)
    bg = sub.add_parser("bridge").add_subparsers(dest="bridge_cmd", required=True)
    bl = bg.add_parser("list"); bl.add_argument("--ffe"); bl.add_argument("--zig"); bl.add_argument("--status")
    bs = bg.add_parser("status"); bs.add_argument("id", type=int); bs.add_argument("status")
    for p in (bl, bs):
        p.add_argument("--db", default=DEFAULT_DB)
    a = ap.parse_args(argv)
    if a.cmd == "research":
        return research_cmd(a)
    if a.cmd == "bridge":
        return bridge_cmd(a)
    if a.cmd == "eval":
        return eval_cmd(a)
    if a.cmd == "agents":
        return agents_cmd(a)
    if a.cmd == "emu":
        from .harness import bringup
        if a.emu_cmd == "monitor":
            print(bringup.monitor(a.command, a.monitor_port))
            return 0
        r = bringup.calibrate(dbmod.connect(a.db), a.exe, a.binary, port=a.gdb_port, limit_mb=a.limit_mb,
                              progress=lambda n: print(f"  searched {n >> 20} MB", flush=True))
        print(json.dumps(r, indent=2))
        if r["ok"]:
            emu = {"binary": a.binary, "code_base": r["code_base"], "gdb_port": a.gdb_port, "monitor_port": 4444}
            print(f"\nFFELAB_EMU='{json.dumps({k: v for k, v in emu.items() if v is not None})}'")
        return 0 if r["ok"] else 1
    if a.cmd == "scenario":
        return scenario_cmd(a)
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
    elif a.cmd == "ref":
        from . import fe2ref
        con = dbmod.connect(a.db)
        if a.ref_cmd == "import":
            m = fe2ref.matcher(a.fe2, a.src)
            rows = m.run()
            print(json.dumps({"matched": fe2ref.store(con, rows), "holdout": m.holdout(),
                              "by_how": dict(Counter(r["how"] for r in rows)),
                              "with_ref_name": sum(1 for r in rows if r["ref_name"])}, indent=2))
        else:
            q = "SELECT * FROM ref_matches" + (" WHERE func=?" if a.func is not None else "") + " ORDER BY func"
            for r in con.execute(q, () if a.func is None else (a.func,)):
                print(f"FUNC_{r['func']:06d}  {r['ref']}:{r['ref_label']}  {r['how']}"
                      f"  csim={r['csim']:.2f}  conf={r['confidence']:.2f}")
    elif a.cmd == "db":
        counts = dbmod.copy(dbmod.connect(a.src), dbmod.connect(a.dst))
        print(json.dumps(counts, indent=2))
        print("embeddings are not copied; run `ffelab embed --db DEST`")
    elif a.cmd == "uncertain":
        led = Ledger(dbmod.connect(a.db))
        for f, h in led.most_uncertain(a.n):
            print(f"FUNC_{f:06d}  entropy={h:.2f} bits")


def research_cmd(a) -> int:
    from . import research as rs
    led = Ledger(dbmod.connect(a.db))
    try:
        if a.research_cmd == "view":
            r = rs.view(led, rs.func_num(a.func), a.max_lines)
            asm = r.pop("asm")
            print(json.dumps(r, indent=2)); print(asm)
            return 0
        if a.research_cmd == "refs":
            r = rs.refs(led.con, rs.data_num(a.data))
        elif a.research_cmd == "grep":
            r = rs.grep(led.con, a.pattern, a.n)
        elif a.research_cmd == "claim":
            r = rs.claim(led, rs.func_num(a.func), a.label, a.confidence, a.source, a.basis)
        elif a.research_cmd == "import":
            r = rs.import_claims(led, a.file, a.source)
        elif a.research_cmd == "subsystem":
            from . import bridge as br
            r = br.subsystem(led, a.spec, a.min_prob)
        elif a.research_cmd == "cut-check":
            from . import bridge as br
            r = br.cut_check(led, rs.func_num(a.func), a.min_prob)
        elif a.research_cmd == "link":
            from . import bridge as br
            r = br.link(led, a.ffe, a.zig, a.relation, a.note, a.source,
                        br.ZigRepo(a.zig_root) if a.zig_root else None)
        else:
            r = rs.retag(led.con, a.old, a.new)
    except (ValueError, KeyError) as e:
        print(f"refused: {e}")
        return 1
    print(json.dumps(r, indent=2))
    return 0


def bridge_cmd(a) -> int:
    from . import bridge as br
    con = dbmod.connect(a.db)
    try:
        r = (br.links(con, a.ffe, a.zig, a.status) if a.bridge_cmd == "list"
             else br.set_status(con, a.id, a.status))
    except (ValueError, KeyError) as e:
        print(f"refused: {e}")
        return 1
    print(json.dumps(r, indent=2))
    return 0


def scenario_cmd(a) -> int:
    from . import behavior
    from .harness import bringup
    led = Ledger(dbmod.connect(a.db))
    if a.scenario_cmd == "list":
        have = bringup.snapshots(a.monitor_port)
        for name, tags in sorted(behavior.scenarios(led).items()):
            mark = "" if not have else ("  snapshot ok" if name in have else "  NO SNAPSHOT in QEMU")
            print(f"{name:16} {','.join(sorted(tags))}{mark}")
        if not have:
            print("(QEMU monitor not reachable, so snapshots weren't checked)")
        return 0
    tags = [t.strip() for t in a.tags.split(",") if t.strip()]
    if a.scenario_cmd == "save":
        bringup.save_state(a.name, a.monitor_port)
        print(f"saved QEMU snapshot {a.name}")
    behavior.register_scenario(led, a.name, tags, a.notes)
    print(f"registered {a.name}: {', '.join(sorted(set(tags)))}")
    return 0


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


def agents_cmd(a) -> int:
    from .orchestrator import questions as qs
    led = Ledger(dbmod.connect(a.db))
    qs.ensure_schema(led.con)
    if a.agents_cmd == "run":
        from .harness.factory import make_backend
        from .orchestrator import tracing
        from .orchestrator.budget import Budget
        from .orchestrator.orchestrator import Orchestrator, run
        from .orchestrator.workers import ClaudeWorker, HeuristicWorker
        budget = Budget(max_usd=a.max_usd, max_tokens=a.max_tokens, max_seconds=a.max_minutes * 60,
                        max_rounds=a.rounds, worker_max_turns=a.worker_max_turns,
                        worker_max_usd=a.worker_max_usd, max_open_questions=a.max_questions)
        if a.worker == "claude":
            from .evals.solvers import DEFAULT_MODEL
            worker = ClaudeWorker(led, a.model or DEFAULT_MODEL, a.effort)
        else:
            worker = HeuristicWorker(led)
        provider = tracing.setup(a.trace)
        try:
            rep = run(Orchestrator(led, make_backend(a.backend, led.con), worker, budget, a.blind, a.attacks,
                                   a.concurrency, progress=lambda m: print(m, flush=True)))
        finally:
            tracing.shutdown(provider)
        s = rep.spend
        print(f"stopped: {rep.stop} after {len(rep.rounds)} rounds; ${s.usd:.2f}, "
              f"{s.input_tokens} in / {s.output_tokens} out tokens, {s.workers} workers ({s.failed_workers} failed)")
        if rep.open_questions:
            print(f"{len(rep.open_questions)} questions waiting: ffelab agents questions")
        if a.out:
            a.out.parent.mkdir(parents=True, exist_ok=True)
            a.out.write_text(rep.model_dump_json(indent=1))
            print(f"report: {a.out}")
        return 0
    if a.agents_cmd == "questions":
        rows = qs.all_questions(led.con) if a.all else qs.open_questions(led.con)
        for q in rows:
            func = f" FUNC {q.func}" if q.func is not None else ""
            print(f"[{q.id}] {q.kind}{func} ({q.source}, {q.status}): {q.question}")
            if q.answer:
                print(f"     answer: {q.answer}")
        if not rows:
            print("no questions waiting")
        return 0
    if a.agents_cmd == "answer":
        q = qs.answer(led, a.id, a.text, a.claim, a.confidence, a.close)
        print(f"[{q.id}] answered")
        return 0
    if a.agents_cmd == "dismiss":
        qs.dismiss(led.con, a.id)
        print(f"[{a.id}] dismissed")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
