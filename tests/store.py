"""A fresh, empty store for each test, on whichever backend the run targets.

    pytest                                              # SQLite, in memory
    FFELAB_TEST_PG=postgresql:///ffelab_test pytest     # the same tests on Postgres

On Postgres each store is its own schema (namespace) in that database,
dropped when the run ends, so tests never see each other's rows.
"""
import atexit
import os
import uuid

from ffelab import db as dbmod

PG_URL = os.environ.get("FFELAB_TEST_PG")
_schemas: list[str] = []


def fresh_db():
    if not PG_URL:
        return dbmod.connect(":memory:")
    return dbmod.connect(pg_schema_url(new_schema()))


def new_schema() -> str:
    import psycopg
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PG_URL, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA {name}")
    _schemas.append(name)
    return name


def pg_schema_url(schema: str) -> str:
    sep = "&" if "?" in PG_URL else "?"
    return f"{PG_URL}{sep}options=-csearch_path%3D{schema},public"


@atexit.register
def _drop():
    if not _schemas:
        return
    import psycopg
    with psycopg.connect(PG_URL, autocommit=True) as c:
        for s in _schemas:
            c.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")


def shared_store():
    """A function returning new connections to one shared store (agents in separate
    processes each hold their own connection)."""
    if PG_URL:
        url = pg_schema_url(new_schema())
    else:
        import tempfile
        url = os.path.join(tempfile.mkdtemp(), "shared.db")
    dbmod.connect(url).close()                          # create the schema once
    return lambda: dbmod.connect(url)
