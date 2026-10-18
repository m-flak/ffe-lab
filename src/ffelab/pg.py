"""Postgres backend: a thin adapter that makes a psycopg connection behave like
the ``sqlite3`` connection the rest of ffelab already uses.

Why an adapter and not an ORM: every module talks plain SQL through
``con.execute(sql, params)`` with ``?`` placeholders and ``row["col"]`` access.
The adapter keeps that interface, so the same queries run on both backends and
SQLite stays the zero-setup lite mode (DECISIONS D13).

What it translates, and nothing more:

* ``?`` placeholders become psycopg's ``%s`` (and a literal ``%`` becomes ``%%``).
* ``CURRENT_TIMESTAMP`` becomes ``ffelab_now()``, which returns the same
  ``YYYY-MM-DD HH:MM:SS`` UTC text SQLite stores, so timestamps compare the same
  way on both backends (``resolve_bet`` compares them).
* Python ``bool`` is sent as an integer, as SQLite stores it.
* Rows support ``row["col"]``, ``row[0]`` and ``dict(row)``, like ``sqlite3.Row``.

Transactions follow Python's sqlite3 module: reads run on their own, the first
write opens a transaction and ``commit()`` ends it. So a connection that only
reads never sits "idle in transaction" holding locks.

Everything else (upserts, ``RETURNING id``) is written in SQL both databases
understand, at the call site.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

try:
    import psycopg
    from psycopg.adapt import Dumper
except ImportError as e:                                    # pragma: no cover
    raise ImportError("Postgres support needs psycopg: pip install -e '.[pg]'") from e


class Row(dict):
    """A dict that also answers ``row[0]``, like ``sqlite3.Row``."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _row_factory(cursor):
    names = [c.name for c in cursor.description] if cursor.description else []
    return lambda values: Row(zip(names, values))


class _BoolAsInt(Dumper):
    oid = psycopg.adapters.types["int8"].oid

    def dump(self, obj):
        return b"1" if obj else b"0"


def translate(sql: str, has_params: bool) -> str:
    """sqlite3 SQL -> psycopg SQL. Leaves quoted strings alone except for ``%``."""
    out, i, n = [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":                                       # copy a string literal ('' is an escaped quote)
            j = i + 1
            while True:
                k = sql.find("'", j)
                if k == -1:
                    k = n - 1
                    break
                if k + 1 < n and sql[k + 1] == "'":
                    j = k + 2
                    continue
                break
            lit = sql[i:k + 1]
            out.append(lit.replace("%", "%%") if has_params else lit)
            i = k + 1
            continue
        if ch == "?":
            out.append("%s")
        elif ch == "%" and has_params:
            out.append("%%")
        else:
            out.append(ch)
        i += 1
    return "".join(out).replace("CURRENT_TIMESTAMP", "ffelab_now()")


_WRITES = ("insert", "update", "delete", "replace", "create", "alter", "drop")


class PgConnection:
    dialect = "postgres"
    hold = 0                                                # > 0 inside db.atomic(): commit() waits

    def __init__(self, raw: "psycopg.Connection"):
        self.raw = raw
        raw.autocommit = True                               # we BEGIN ourselves, like sqlite3
        raw.row_factory = _row_factory
        raw.adapters.register_dumper(bool, _BoolAsInt)
        self.in_transaction = False
        self.row_factory = None                             # accepted and ignored (sqlite3 compatibility)

    # -------------------------------------------------------------- transactions
    def begin(self) -> None:
        if not self.in_transaction:
            self.raw.execute("BEGIN")
            self.in_transaction = True

    def commit(self) -> None:
        if self.in_transaction and not self.hold:
            self.raw.execute("COMMIT")
            self.in_transaction = False

    def rollback(self) -> None:
        if self.in_transaction:
            self.raw.execute("ROLLBACK")
            self.in_transaction = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        self.rollback() if exc_type else self.commit()
        return False

    def close(self) -> None:
        self.rollback()
        self.raw.close()

    # -------------------------------------------------------------- statements
    def _maybe_begin(self, sql: str) -> None:
        if sql.lstrip().split(None, 1)[0].lower() in _WRITES:
            self.begin()

    def execute(self, sql: str, params: Sequence[Any] | None = ()) -> "psycopg.Cursor":
        self._maybe_begin(sql)
        cur = self.raw.cursor()
        try:
            if params:
                cur.execute(translate(sql, True), tuple(params))
            else:
                cur.execute(translate(sql, False))
        except Exception:
            if not self.hold:                               # a failed statement aborts the transaction;
                self.rollback()                             # inside atomic() the block's exit rolls back
            raise
        return cur

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> "psycopg.Cursor":
        self._maybe_begin(sql)
        cur = self.raw.cursor()
        try:
            cur.executemany(translate(sql, True), [tuple(p) for p in seq])
        except Exception:
            if not self.hold:
                self.rollback()
            raise
        return cur

    def executescript(self, script: str) -> None:
        self.begin()
        try:
            self.raw.execute(translate(script, False))
        except Exception:
            self.rollback()
            raise
        self.commit()


def connect(url: str) -> PgConnection:
    return PgConnection(psycopg.connect(url))
