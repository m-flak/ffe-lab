"""The store: static facts about the binary plus the evidence ledger.

Everything the agent knows lives here, never only in a conversation, so every
cycle of the loop can start fresh from the current state.

Two backends, one schema (DECISIONS D13):

* **SQLite** (lite mode): ``connect("ffe.db")``. One file, no server; right for
  one person and one agent at a time.
* **Postgres**: ``connect("postgresql://localhost/ffelab")``. For several agents
  writing at once, and for pgvector similarity search (``embed.py``).

``SCHEMA`` below is written once, in SQLite's dialect, and rendered for
Postgres by ``pg_schema()``. Queries elsewhere are plain SQL both accept.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
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
    created TEXT DEFAULT CURRENT_TIMESTAMP,
    run_key TEXT                  -- unique per campaign step (e.g. workflow id + step), so a retry can't record twice
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
    "binary" TEXT,                -- quoted: BINARY is a Postgres keyword
    func INTEGER, pos INTEGER, confidence REAL, method TEXT,
    PRIMARY KEY ("binary", func)
);
CREATE TABLE IF NOT EXISTS names (
    func INTEGER PRIMARY KEY, name TEXT, prob REAL, provenance TEXT,
    written TEXT DEFAULT CURRENT_TIMESTAMP
);
-- call-graph structure (graph.py) -----------------------------------------
CREATE TABLE IF NOT EXISTS graph_features (
    func INTEGER PRIMARY KEY, fan_in INTEGER, fan_out INTEGER,
    span REAL,                    -- median link-order distance to call partners
    height INTEGER,               -- longest call chain below (0 = calls nothing), cycles collapsed
    depth INTEGER,                -- longest call chain above
    cycle INTEGER, cycle_size INTEGER   -- the cycle (strongly connected component) it sits in, if any
);
-- similarity search (embed.py) ---------------------------------------------
CREATE TABLE IF NOT EXISTS embeddings (
    func INTEGER, model TEXT,     -- model: which embedder made the vector, e.g. asm-ngram-v1
    kind TEXT,                    -- asm | fingerprint | graph
    dim INTEGER,
    vec VECTOR,                   -- pgvector `vector` on Postgres; float32 bytes on SQLite
    created TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (func, model)
);
"""

# indexes on columns that MIGRATIONS may have just added
INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS experiments_run_key ON experiments(run_key);
"""

# columns added after v0.1; ALTERed into older databases on connect
MIGRATIONS = {
    "functions": [("asm_text", "TEXT")],
    "evidence": [("experiment_id", "INTEGER")],
    "bets": [("kind", "TEXT DEFAULT 'prediction'"), ("label", "TEXT"), ("spec", "TEXT"),
             ("experiment_id", "INTEGER"), ("parent_id", "INTEGER")],
    "experiments": [("run_key", "TEXT")],
}

PG_NOW = """
CREATE OR REPLACE FUNCTION ffelab_now() RETURNS text LANGUAGE sql STABLE AS
$$ SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') $$;
"""


def is_postgres_url(target: str | Path) -> bool:
    return str(target).startswith(("postgres://", "postgresql://"))


def dialect(con) -> str:
    """"postgres" or "sqlite"."""
    return getattr(con, "dialect", "sqlite")


class SqliteConnection(sqlite3.Connection):
    """sqlite3 connection whose ``commit()`` waits while an ``atomic()`` block is open."""
    dialect = "sqlite"
    hold = 0

    def commit(self) -> None:
        if not self.hold:
            super().commit()


def connect(target: str | Path):
    """Open (and create or migrate) the store. A ``postgresql://`` URL selects
    Postgres; anything else is a SQLite file path (or ``:memory:``)."""
    if is_postgres_url(target):
        return _connect_pg(str(target))
    con = sqlite3.connect(str(target), timeout=30, factory=SqliteConnection)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.replace(" VECTOR,", " BLOB,"))
    _migrate(con)
    con.executescript(INDEXES)
    con.commit()
    return con


def pg_schema(vector: bool = True) -> str:
    """SCHEMA in Postgres' dialect. Without the pgvector extension, vectors are
    stored as bytes and searched in Python, exactly as on SQLite."""
    s = SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY")
    s = re.sub(r"\bREAL\b", "DOUBLE PRECISION", s)      # Postgres REAL is 4-byte; SQLite's is 8
    return s.replace(" VECTOR,", " vector," if vector else " BYTEA,")


def _connect_pg(url: str):
    from .pg import connect as pg_connect
    con = pg_connect(url)
    try:                                                   # needs the extension installed on the server
        con.raw.execute("CREATE EXTENSION IF NOT EXISTS vector")
        vector = True
    except Exception:
        vector = False
    con.begin()
    con.execute("SELECT pg_advisory_xact_lock(7302)")      # agents starting together create the schema once
    con.raw.execute(PG_NOW)
    con.raw.execute(_pg_sql(pg_schema(vector)))
    _migrate(con)
    con.raw.execute(INDEXES)
    con.commit()
    return con


def _pg_sql(sql: str) -> str:
    from .pg import translate
    return translate(sql, False)


def columns(con, table: str) -> list[str]:
    """Column names of ``table``, in order, on either backend."""
    if dialect(con) == "postgres":
        return [r["column_name"] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema()"
            " AND table_name=? ORDER BY ordinal_position", (table,))]
    return [r["name"] for r in con.execute(f"PRAGMA table_info({table})")]


def _migrate(con) -> None:
    for table, cols in MIGRATIONS.items():
        have = set(columns(con, table))
        for col, decl in cols:
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def lock(con, namespace: str, key: int) -> None:
    """Serialise read-modify-write on one thing (e.g. one function's belief)
    across agents, until the next ``commit()``.

    Postgres: a transaction-scoped advisory lock on (namespace, key), so agents
    working on different functions never wait for each other. SQLite: take the
    database write lock now (``BEGIN IMMEDIATE``) instead of at the first write,
    so a second process can't read the same stale belief in between."""
    if dialect(con) == "postgres":
        con.begin()
        con.execute("SELECT pg_advisory_xact_lock(hashtext(?), ?)", (namespace, int(key)))
    elif not con.in_transaction:
        con.execute("BEGIN IMMEDIATE")


def ingest(con, funcs: list[Function], tables: list[Table]) -> None:
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


@contextmanager
def atomic(con):
    """All writes inside the block land together or not at all.

    Every ``commit()`` inside (the ledger commits after each write) waits for the
    end of the block; an exception rolls the whole block back. Nested blocks join
    the outer one. Locks taken inside (``lock``) are held until the end, so keep
    blocks to one experiment's worth of work."""
    if con.hold:
        yield con
        return
    if dialect(con) == "postgres":
        con.begin()
    elif not con.in_transaction:
        con.execute("BEGIN IMMEDIATE")
    con.hold = 1
    try:
        yield con
    except BaseException:
        con.hold = 0
        con.rollback()
        raise
    con.hold = 0
    con.commit()


# ---------------------------------------------------------------- moving between backends
TABLES = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA)


def copy(src, dst) -> dict[str, int]:
    """Copy every row from one store into another, empty one (SQLite -> Postgres
    to scale up, or back to a single file to share). Ids are kept, so every
    reference (bet -> experiment, evidence -> experiment, ...) still holds.
    Embeddings are skipped: rebuild them on the destination with ``ffelab embed``."""
    busy = [t for t in TABLES if dst.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()]
    if busy:
        raise ValueError(f"destination is not empty ({', '.join(busy)}); copy needs an empty store")
    counts = {}
    for t in TABLES:
        if t == "embeddings":
            continue
        cols = [c for c in columns(src, t) if c in set(columns(dst, t))]
        names = ", ".join(f'"{c}"' for c in cols)
        rows = [tuple(r[c] for c in cols) for r in src.execute(f"SELECT {names} FROM {t}")]
        if rows:
            dst.executemany(f"INSERT INTO {t} ({names}) VALUES ({','.join('?' * len(cols))})", rows)
        counts[t] = len(rows)
    if dialect(dst) == "postgres":                         # next generated id continues after the copied ones
        for t in TABLES:
            if "id" in columns(dst, t) and dst.execute(
                    "SELECT pg_get_serial_sequence(?, 'id') AS s", (t,)).fetchone()["s"]:
                dst.execute(f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), "
                            f"COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {t}")
    dst.commit()
    return counts
