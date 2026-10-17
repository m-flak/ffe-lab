"""Embeddings and "find functions like this one".

An *embedding* is a fixed-length list of numbers describing something, made so
that similar things get vectors pointing the same way. Similarity is then the
cosine of the angle between two vectors (1 = same direction, 0 = unrelated).

Two built-in embedders, both deterministic, local and free (no API key):

* ``asm-ngram-v1`` (kind ``asm``): the function's instructions are normalised
  (addresses, constants, function and data names become placeholders), cut into
  runs of 1-3 consecutive instructions ("n-grams"), and each run is hashed into
  one of 256 slots (the "hashing trick"). Two functions sharing many instruction
  patterns get similar vectors. Normalisation drops inherited names, so a
  match never leaks a hacker's name (or any belief) to the blind labeller.
* ``fp-scenario-v1`` (kind ``fingerprint``): the function's hit rate in each
  tagged scenario (``behavior.py`` fingerprints), hashed into 64 slots.
  Functions that fire in the same situations point the same way. Rebuild after
  new logging passes.

Embeddings are not evidence: they never touch beliefs or bets. They only help
an agent decide where to look; experiments still settle everything (D3, D13).

Storage: on Postgres with pgvector the ``vec`` column is a ``vector`` and the
search runs in the database, through an HNSW index (a graph of near
neighbours that finds close vectors without comparing against every row). On
SQLite, or Postgres without the extension, vectors are float32 bytes and the
search is a NumPy scan, which at ~1,600 functions takes milliseconds.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Callable

import numpy as np

from . import db

FUNC_RE = re.compile(r"\b_?func_\d+(?:_\w+)?")
DATA_RE = re.compile(r"\b_?data_\w+")
NUM_RE = re.compile(r"\b(?:0x[0-9a-f]+|[0-9][0-9a-f]*h|\d+)\b")
JUMP_RE = re.compile(r"^(j\w+|loop\w*)\s+(?:near\s+|short\s+)?(?!FUNC\b)[\w.$@]+$")


def normalise(ins: str) -> str:
    """One instruction with names and numbers replaced by placeholders."""
    s = " ".join(ins.lower().split()).replace(", ", ",")
    s = DATA_RE.sub("DATA", FUNC_RE.sub("FUNC", s))
    s = NUM_RE.sub("N", s)
    if (m := JUMP_RE.match(s)):
        s = f"{m.group(1)} LBL"                        # local jump target
    return s


def _slot(token: str, dim: int) -> tuple[int, float]:
    h = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "little")
    return h % dim, (1.0 if (h >> 63) & 1 else -1.0)  # a random sign keeps collisions from piling up


def _hashed(counts: Counter, dim: int) -> np.ndarray | None:
    v = np.zeros(dim, dtype=np.float32)
    for tok, c in counts.items():
        i, sign = _slot(tok, dim)
        v[i] += sign * c
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else None


def asm_vector(con, func: int, dim: int = 256) -> np.ndarray | None:
    row = con.execute("SELECT asm_text FROM functions WHERE num=?", (func,)).fetchone()
    if row is None or not row["asm_text"]:
        return None
    ins = [normalise(x) for x in row["asm_text"].splitlines() if x.strip()]
    grams = Counter()
    for n in (1, 2, 3):
        for i in range(len(ins) - n + 1):
            grams[f"{n}|" + " ; ".join(ins[i:i + n])] += 1
    grams.update(f"op|{x.split()[0]}" for x in ins)
    return _hashed(Counter({k: 1 + math.log(c) for k, c in grams.items()}), dim)


def fingerprint_vector(con, func: int, dim: int = 64) -> np.ndarray | None:
    acc: dict[str, list[int]] = {}
    for r in con.execute("SELECT scenario, hits, frames FROM fingerprints WHERE func=?", (func,)):
        a = acc.setdefault(r["scenario"], [0, 0])
        a[0] += r["hits"]; a[1] += r["frames"]
    rates = Counter({f"sc|{sc}": math.sqrt(h / max(fr, 1)) for sc, (h, fr) in acc.items()})
    return _hashed(rates, dim) if rates else None


@dataclass(frozen=True)
class Embedder:
    kind: str
    dim: int
    fn: Callable[..., np.ndarray | None]
    funcs_sql: str


MODELS: dict[str, Embedder] = {
    "asm-ngram-v1": Embedder("asm", 256, asm_vector, "SELECT num AS f FROM functions ORDER BY num"),
    "fp-scenario-v1": Embedder("fingerprint", 64, fingerprint_vector,
                               "SELECT DISTINCT func AS f FROM fingerprints ORDER BY f"),
}
DEFAULT_MODEL = "asm-ngram-v1"


# ---------------------------------------------------------------- storage
def has_pgvector(con) -> bool:
    if db.dialect(con) != "postgres":
        return False
    row = con.execute("SELECT udt_name FROM information_schema.columns WHERE table_schema=current_schema()"
                      " AND table_name='embeddings' AND column_name='vec'").fetchone()
    return row is not None and row["udt_name"] == "vector"


def _encode(con, v: np.ndarray):
    if has_pgvector(con):
        return "[" + ",".join(f"{x:.7g}" for x in v) + "]"
    return np.asarray(v, dtype="<f4").tobytes()


def _decode(raw) -> np.ndarray:
    if isinstance(raw, str):                           # pgvector's text form "[0.1,0.2,...]"
        return np.array([float(x) for x in raw.strip("[]").split(",")], dtype=np.float32)
    return np.frombuffer(bytes(raw), dtype="<f4")


def _model(name: str) -> Embedder:
    if name not in MODELS:
        raise ValueError(f"unknown embedding model {name!r}; known: {', '.join(MODELS)}")
    return MODELS[name]


def build(con, model: str = DEFAULT_MODEL, funcs: list[int] | None = None) -> int:
    """(Re)compute embeddings for ``funcs`` (default: every function the model covers)."""
    emb = _model(model)
    funcs = funcs if funcs is not None else [r["f"] for r in con.execute(emb.funcs_sql)]
    vec_param = "?::vector" if has_pgvector(con) else "?"
    rows = []
    for f in funcs:
        v = emb.fn(con, f, emb.dim)
        if v is not None:
            rows.append((f, model, emb.kind, emb.dim, _encode(con, v)))
    con.executemany(
        f"INSERT INTO embeddings (func, model, kind, dim, vec) VALUES (?,?,?,?,{vec_param})"
        " ON CONFLICT (func, model) DO UPDATE SET kind=excluded.kind, dim=excluded.dim,"
        " vec=excluded.vec, created=CURRENT_TIMESTAMP", rows)
    con.commit()
    ensure_index(con, model)
    return len(rows)


def ensure_index(con, model: str) -> None:
    """HNSW index for one model on pgvector (no-op elsewhere). One partial index
    per model, because an index needs a fixed vector length."""
    if not has_pgvector(con):
        return
    emb = _model(model)
    name = "embeddings_hnsw_" + re.sub(r"\W", "_", model)
    con.execute(f"CREATE INDEX IF NOT EXISTS {name} ON embeddings USING hnsw "
                f"((vec::vector({emb.dim})) vector_cosine_ops) WHERE model = '{model}'")
    con.commit()


# ---------------------------------------------------------------- search
def similar(con, func: int, model: str = DEFAULT_MODEL, k: int = 10) -> list[dict]:
    """The ``k`` functions whose vectors are closest to ``func``'s, best first.

    Returns function numbers and cosine similarity only: no names, labels or
    beliefs, so it is safe to show to the blind labeller."""
    emb = _model(model)
    me = con.execute("SELECT vec FROM embeddings WHERE func=? AND model=?", (func, model)).fetchone()
    if me is None:
        raise KeyError(f"FUNC {func} has no {model} embedding; run `ffelab embed --model {model}`")
    if has_pgvector(con):
        # model is a known name (checked above), inlined so the planner can match the partial index
        cast = f"vector({emb.dim})"
        rows = con.execute(
            f"SELECT func, 1 - (vec::{cast} <=> ?::{cast}) AS score FROM embeddings"
            f" WHERE model = '{model}' AND func <> ? ORDER BY vec::{cast} <=> ?::{cast} LIMIT ?",
            (str(me["vec"]), func, str(me["vec"]), k)).fetchall()
        return [{"func": r["func"], "score": round(float(r["score"]), 4)} for r in rows]
    q = _decode(me["vec"])
    rows = con.execute("SELECT func, vec FROM embeddings WHERE model=? AND func<>?", (model, func)).fetchall()
    if not rows:
        return []
    m = np.stack([_decode(r["vec"]) for r in rows])
    scores = m @ q                                     # vectors are unit length, so this is the cosine
    top = np.argsort(-scores)[:k]
    return [{"func": rows[i]["func"], "score": round(float(scores[i]), 4)} for i in top]
