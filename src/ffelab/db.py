"""SQLite store: static facts about the binary plus the evidence ledger.

Everything the agent knows lives here, never only in a conversation, so every
cycle of the loop can start fresh from the current state.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .asm import Function
from .tables import Table

SCHEMA = """
CREATE TABLE IF NOT EXISTS functions (
    num INTEGER PRIMARY KEY,
    pos INTEGER,
    ord INTEGER,                 -- index in address order
    name TEXT,                   -- inherited hacker name (unverified)
    prefix TEXT,
    n_ins INTEGER,
    has_frame INTEGER,
    aligned4 INTEGER,
    filler_tail INTEGER,
    asm_text TEXT
);
CREATE TABLE IF NOT EXISTS calls (
    caller INTEGER, callee INTEGER, kind TEXT   -- direct | tail
);
CREATE INDEX IF NOT EXISTS calls_callee ON calls(callee);
CREATE INDEX IF NOT EXISTS calls_caller ON calls(caller);
CREATE TABLE IF NOT EXISTS data_refs (func INTEGER, data INTEGER);
CREATE TABLE IF NOT EXISTS tables (
    label TEXT PRIMARY KEY, data_num INTEGER, comment TEXT, kind TEXT, slots TEXT
);
CREATE TABLE IF NOT EXISTS segments (
    seg INTEGER PRIMARY KEY, first_ord INTEGER, last_ord INTEGER,
    first_num INTEGER, last_num INTEGER, label TEXT, anchors TEXT,
    boundary_conf REAL            -- confidence that the boundary *before* this segment is real
);

-- ledger -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS beliefs (
    func INTEGER, label TEXT, prob REAL, PRIMARY KEY (func, label)
);
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    func INTEGER, source TEXT, kind TEXT, likelihoods TEXT, detail TEXT,
    experiment_id INTEGER,
    created TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT,                    -- logging_pass | halt | unicorn | static_check
    scenario TEXT,
    detail TEXT,                  -- JSON: funcs, counts, snapshot, ...
    created TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT, domain TEXT, func INTEGER, claim TEXT, prediction TEXT,
    prob REAL, outcome INTEGER, brier REAL,
    created TEXT DEFAULT CURRENT_TIMESTAMP, resolved TEXT,
    kind TEXT DEFAULT 'prediction',   -- claim | rival | prediction
    label TEXT,                        -- for claim/rival bets: the asserted label
    spec TEXT,                         -- JSON machine-checkable prediction
    experiment_id INTEGER,             -- the experiment that resolved it
    parent_id INTEGER                  -- rival bets point at the claim they attack
);
CREATE TABLE IF NOT EXISTS contradictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    func INTEGER, bet_id INTEGER, evidence_id INTEGER,
    claimed TEXT, favoured TEXT, ratio REAL, detail TEXT,
    status TEXT DEFAULT 'open',        -- open | explained | claim_withdrawn
    created TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS blind_labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    func INTEGER, source TEXT, label TEXT, confidence REAL,
    revealed INTEGER DEFAULT 0, agrees INTEGER,
    created TEXT DEFAULT CURRENT_TIMESTAMP
);
-- open labels, scenarios, fingerprints, knockouts -------------------------
CREATE TABLE IF NOT EXISTS vocabulary (
    path TEXT PRIMARY KEY, parent TEXT, description TEXT, created_by TEXT,
    created TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS func_labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    func INTEGER, path TEXT, source TEXT, confidence REAL, experiment_id INTEGER, note TEXT,
    created TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS scenarios (
    name TEXT PRIMARY KEY, tags TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS fingerprints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    func INTEGER, scenario TEXT, hits INTEGER, frames INTEGER, rate REAL, experiment_id INTEGER
);
CREATE INDEX IF NOT EXISTS fp_func ON fingerprints(func);
CREATE TABLE IF NOT EXISTS knockouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER, func INTEGER, scenario TEXT, health TEXT,
    changed_frac REAL, bbox TEXT, baseline TEXT, knocked TEXT, diff_image TEXT,
    created TEXT DEFAULT CURRENT_TIMESTAMP
);
-- executables and per-binary address maps --------------------------------
CREATE TABLE IF NOT EXISTS binaries (
    id TEXT PRIMARY KEY, path TEXT, size INTEGER, verdict TEXT, match_rate REAL,
    created TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS addr_maps (
    binary TEXT, func INTEGER, pos INTEGER, confidence REAL, method TEXT,
    PRIMARY KEY (binary, func)
);
CREATE TABLE IF NOT EXISTS names (
    func INTEGER PRIMARY KEY, name TEXT, prob REAL, provenance TEXT,
    written TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

# columns added after v0.1; ALTERed into older databases on connect
MIGRATIONS = {
    "functions": [("asm_text", "TEXT")],
    "evidence": [("experiment_id", "INTEGER")],
    "bets": [("kind", "TEXT DEFAULT 'prediction'"), ("label", "TEXT"), ("spec", "TEXT"),
             ("experiment_id", "INTEGER"), ("parent_id", "INTEGER")],
}


def connect(path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols:
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    con.commit()
    return con


def ingest(con: sqlite3.Connection, funcs: list[Function], tables: list[Table]) -> None:
    ordered = sorted((f for f in funcs if f.pos is not None), key=lambda f: f.pos)
    ord_of = {f.num: i for i, f in enumerate(ordered)}
    con.execute("DELETE FROM functions"); con.execute("DELETE FROM calls")
    con.execute("DELETE FROM data_refs"); con.execute("DELETE FROM tables")
    con.executemany(
        "INSERT INTO functions (num,pos,ord,name,prefix,n_ins,has_frame,aligned4,filler_tail,asm_text)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(f.num, f.pos, ord_of.get(f.num), f.name, f.prefix(), len(f.instructions),
          int(f.has_frame), None if f.aligned4 is None else int(f.aligned4), f.filler_tail,
          "\n".join(f.instructions))
         for f in funcs])
    rows = []
    for f in funcs:
        rows += [(f.num, c, "direct") for c in f.calls]
        rows += [(f.num, t, "tail") for t in f.tail_jumps]
    con.executemany("INSERT INTO calls VALUES (?,?,?)", rows)
    con.executemany("INSERT INTO data_refs VALUES (?,?)",
                    [(f.num, d) for f in funcs for d in set(f.data_refs)])
    con.executemany("INSERT INTO tables VALUES (?,?,?,?,?)",
                    [(t.label, t.data_num, t.comment, t.kind, json.dumps(t.slots)) for t in tables])
    con.commit()
