"""MCP server for a static researcher (Muse, or any MCP client): read anything,
write claims only. Protocol: ``docs/STATIC_RESEARCH.md``.

Run:  FFELAB_DB=ffe.db FFELAB_SOURCE=ai:muse python -m ffelab.mcp_research

The source is fixed when the server starts, not a tool argument, so the agent
can't file claims under another name. There is no emulator, no bet placing or
resolving, no settling and no SQL: the full ``ffelab.mcp_server`` is for the
experiment agents.
"""
from __future__ import annotations

import os
import threading

try:                                   # mcp >= 2
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:                    # mcp 1.x
    from mcp.server.fastmcp import FastMCP

from . import db as dbmod
from . import labels as lbl
from . import research as rs
from .ledger import Ledger

SOURCE = os.environ.get("FFELAB_SOURCE", "")
if not SOURCE.startswith("ai:"):
    raise SystemExit("set FFELAB_SOURCE to this researcher's source, e.g. FFELAB_SOURCE=ai:muse")

mcp = FastMCP("ffelab-research")
_local = threading.local()


def _led() -> Ledger:
    """One connection per thread: newer MCP SDKs run sync tools in worker threads,
    and a SQLite connection can't cross threads."""
    if not hasattr(_local, "ledger"):
        _local.ledger = Ledger(dbmod.connect(os.environ.get("FFELAB_DB", "ffe.db")))
    return _local.ledger


_led()                                  # fail at start-up, not on the first call, if the store won't open


# ---------------------------------------------------------------- read
@mcp.tool()
def view(func: str) -> dict:
    """Everything known about one function (``1805`` or ``FUNC_001805``): code, callers and
    callees with names, data refs, tables it sits in, segment, call layers, belief, claims."""
    return rs.view(_led(), rs.func_num(func))


@mcp.tool()
def data_refs(data: str) -> list:
    """Functions that touch a data label (``DATA_009252`` or ``9252``)."""
    return rs.refs(_led().con, rs.data_num(data))


@mcp.tool()
def grep_asm(pattern: str, limit: int = 50) -> list:
    """Functions whose asm (names and comments included) matches a regex."""
    return rs.grep(_led().con, pattern, limit)


@mcp.tool()
def uncertain(limit: int = 20) -> list:
    """Functions whose beliefs are flattest: where a good claim helps most."""
    return [{"func": f"FUNC_{f:06d}", "entropy_bits": round(h, 2)} for f, h in _led().most_uncertain(limit)]


@mcp.tool()
def similar_functions(func: str, k: int = 10, model: str = "callgraph-spectral-v1") -> list:
    """Nearest functions by call-graph position (``callgraph-spectral-v1``) or code (``asm-ngram-v1``)."""
    from . import embed
    return embed.similar(_led().con, rs.func_num(func), model, k)


@mcp.tool()
def quotient_outliers() -> list:
    """Calls between labels that nothing else supports: a mislabel or a real cross-layer call."""
    from . import graph
    return graph.outliers(_led())


@mcp.tool()
def vocabulary(under: str | None = None) -> list:
    """The label tree so far, with how many functions carry each path."""
    return lbl.vocabulary(_led().con, under)


@mcp.tool()
def my_claims() -> list:
    """Your claims so far and how each stands (outcome is null until an experiment settles it)."""
    return [dict(r) for r in _led().con.execute(
        "SELECT func, label, prob, outcome, brier FROM bets WHERE source=? AND kind='claim' ORDER BY id", (SOURCE,))]


# ---------------------------------------------------------------- write
@mcp.tool()
def claim(func: str, label: str, confidence: float, basis: str) -> dict:
    """Claim ``func`` is ``label`` (e.g. ``render/texture/palette``) with your honest
    probability that the full path is right. ``basis``: the static facts it rests on.
    One open claim per function; it stands until an experiment settles it."""
    try:
        return rs.claim(_led(), rs.func_num(func), label, confidence, SOURCE, basis)
    except (ValueError, KeyError) as e:                 # a refusal is an answer, not a server error
        return {"refused": str(e)}


if __name__ == "__main__":
    mcp.run()
