"""Open, hierarchical labels.

Beliefs are kept over a small fixed set of top-level subsystems (render,
physics, ai, ...) so the maths stays well-behaved. Anything finer is a *path*
under one of them, e.g. ``render/planet/atmosphere/clouds``. Paths are created
the moment someone (you, an agent, a knockout observation) needs one, and the
vocabulary grows with the project instead of forcing new discoveries into a
fixed box.
"""
from __future__ import annotations

import re
import sqlite3

PATH_RE = re.compile(r"^[a-z0-9_]+(/[a-z0-9_]+)*$")


def top(path: str) -> str:
    return path.split("/", 1)[0]


def normalise(path: str) -> str:
    p = path.strip().strip("/").lower().replace(" ", "_").replace("-", "_")
    p = re.sub(r"/+", "/", p)
    if not PATH_RE.match(p):
        raise ValueError(f"bad label path {path!r}: use lowercase words separated by '/'")
    return p


def validate(path: str, labels: list[str]) -> str:
    p = normalise(path)
    if top(p) not in labels:
        raise ValueError(f"label path must start with one of {labels}, got {p!r}")
    return p


def register(con: sqlite3.Connection, path: str, created_by: str, description: str = "") -> None:
    """Add a path and all its ancestors to the vocabulary (idempotent)."""
    parts = path.split("/")
    for i in range(1, len(parts) + 1):
        sub = "/".join(parts[:i])
        parent = "/".join(parts[:i - 1]) or None
        con.execute("INSERT INTO vocabulary (path, parent, description, created_by) VALUES (?,?,?,?)"
                    " ON CONFLICT (path) DO NOTHING",
                    (sub, parent, description if i == len(parts) else "", created_by))
        if description and i == len(parts):
            con.execute("UPDATE vocabulary SET description=? WHERE path=? AND (description IS NULL OR description='')",
                        (description, sub))
    con.commit()


def vocabulary(con: sqlite3.Connection, under: str | None = None) -> list[dict]:
    q = "SELECT v.path, v.parent, v.description, v.created_by, " \
        "(SELECT COUNT(DISTINCT func) FROM func_labels f WHERE f.path=v.path OR f.path LIKE v.path || '/%') AS functions " \
        "FROM vocabulary v"
    args: tuple = ()
    if under:
        q += " WHERE v.path=? OR v.path LIKE ?"
        args = (under, under + "/%")
    return [dict(r) for r in con.execute(q + " ORDER BY v.path", args)]


def deepest_label(con: sqlite3.Connection, func: int, top_label: str | None = None) -> dict | None:
    """Most specific path recorded for ``func`` (optionally under a top-level label),
    preferring paths backed by an experiment, then by confidence."""
    rows = [dict(r) for r in con.execute("SELECT * FROM func_labels WHERE func=?", (func,))]
    if top_label:
        rows = [r for r in rows if top(r["path"]) == top_label]
    if not rows:
        return None
    return max(rows, key=lambda r: (r["experiment_id"] is not None, r["path"].count("/"), r["confidence"] or 0))
