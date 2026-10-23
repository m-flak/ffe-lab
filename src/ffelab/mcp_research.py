"""MCP server for a static researcher (Muse, or any MCP client): read anything,
write claims only. Protocol: ``docs/STATIC_RESEARCH.md``.

Run:  FFELAB_DB=ffe.db FFELAB_SOURCE=ai:muse python -m ffelab.mcp_research

With ``FFELAB_ZIG_ROOT=/path/to/engine`` the researcher can also read the new
Zig engine (read only, confined to that directory) and propose links between it
and FFE. Links are design proposals in their own table, never evidence.

The source is fixed when the server starts, not a tool argument, so the agent
can't file claims under another name. There is no emulator, no bet placing or
resolving, no settling and no SQL: the full ``ffelab.mcp_server`` is for the
experiment agents.
"""
from __future__ import annotations

import os

try:                                   # mcp >= 2
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:                    # mcp 1.x
    from mcp.server.fastmcp import FastMCP

from . import bridge as br
from . import db as dbmod
from . import labels as lbl
from . import research as rs
from .ledger import Ledger
from .mcp_thread import on_db_thread, tool

SOURCE = os.environ.get("FFELAB_SOURCE", "")
if not SOURCE.startswith("ai:"):
    raise SystemExit("set FFELAB_SOURCE to this researcher's source, e.g. FFELAB_SOURCE=ai:muse")

mcp = FastMCP("ffelab-research")
_ledger = Ledger(on_db_thread(dbmod.connect, os.environ.get("FFELAB_DB", "ffe.db")))   # used only on the tool thread
_zig = br.ZigRepo(os.environ["FFELAB_ZIG_ROOT"]) if os.environ.get("FFELAB_ZIG_ROOT") else None


def _refusing(fn, *args, **kwargs):
    """A refusal is an answer, not a server error."""
    try:
        return fn(*args, **kwargs)
    except (ValueError, KeyError, OSError) as e:
        return {"refused": str(e)}


def _repo() -> br.ZigRepo:
    if _zig is None:
        raise ValueError("no Zig engine configured: set FFELAB_ZIG_ROOT in this server's env")
    return _zig


# ---------------------------------------------------------------- read
@tool(mcp)
def view(func: str) -> dict:
    """Everything known about one function (``1805`` or ``FUNC_001805``): code, callers and
    callees with names, data refs, tables it sits in, segment, call layers, belief, claims."""
    return rs.view(_ledger, rs.func_num(func))


@tool(mcp)
def data_refs(data: str) -> list:
    """Functions that touch a data label (``DATA_009252`` or ``9252``)."""
    return rs.refs(_ledger.con, rs.data_num(data))


@tool(mcp)
def grep_asm(pattern: str, limit: int = 50) -> list:
    """Functions whose asm (names and comments included) matches a regex."""
    return rs.grep(_ledger.con, pattern, limit)


@tool(mcp)
def uncertain(limit: int = 20) -> list:
    """Functions whose beliefs are flattest: where a good claim helps most."""
    return [{"func": f"FUNC_{f:06d}", "entropy_bits": round(h, 2)} for f, h in _ledger.most_uncertain(limit)]


@tool(mcp)
def similar_functions(func: str, k: int = 10, model: str = "callgraph-spectral-v1") -> list:
    """Nearest functions by call-graph position (``callgraph-spectral-v1``) or code (``asm-ngram-v1``)."""
    from . import embed
    return embed.similar(_ledger.con, rs.func_num(func), model, k)


@tool(mcp)
def quotient_outliers() -> list:
    """Calls between labels that nothing else supports: a mislabel or a real cross-layer call."""
    from . import graph
    return graph.outliers(_ledger)


@tool(mcp)
def vocabulary(under: str | None = None) -> list:
    """The label tree so far, with how many functions carry each path."""
    return lbl.vocabulary(_ledger.con, under)


@tool(mcp)
def my_claims() -> list:
    """Your claims so far and how each stands (outcome is null until an experiment settles it)."""
    return [dict(r) for r in _ledger.con.execute(
        "SELECT func, label, prob, outcome, brier FROM bets WHERE source=? AND kind='claim' ORDER BY id", (SOURCE,))]


@tool(mcp)
def subsystem(spec: str, min_prob: float = 0.5) -> dict:
    """An FFE subsystem as a box to cut out: ``seg:12`` or a label path (``render/planet``).
    Members with their belief, entry points called from outside (its real API), what it
    calls outside itself, data shared with the rest of the game, and data only it touches."""
    return _refusing(br.subsystem, _ledger, spec, min_prob)


# ---------------------------------------------------------------- the Zig engine (read only)
@tool(mcp)
def zig_tree(path: str = "", glob: str = "*") -> list | dict:
    """Source files in the Zig engine under ``path``, with line counts (caches and zig-out skipped)."""
    return _refusing(lambda: _repo().tree(path, glob))


@tool(mcp)
def zig_read(path: str, start: int = 1, end: int | None = None) -> dict:
    """Numbered lines of one Zig engine file (at most 400 per call)."""
    return _refusing(lambda: _repo().read(path, start, end))


@tool(mcp)
def zig_grep(pattern: str, glob: str = "*.zig", limit: int = 50) -> list | dict:
    """Lines in the Zig engine matching a regex, as path, line and text."""
    return _refusing(lambda: _repo().grep(pattern, glob, limit))


@tool(mcp)
def zig_outline(path: str) -> list | dict:
    """Declarations in one Zig file: fns, types, top-level consts and tests, with line numbers."""
    return _refusing(lambda: _repo().outline(path))


@tool(mcp)
def links(ffe: str | None = None, zig: str | None = None, status: str | None = None) -> list:
    """Proposed and accepted FFE <-> Zig links, filtered by FFE ref, Zig path prefix or status."""
    return br.links(_ledger.con, ffe, zig, status)


# ---------------------------------------------------------------- write
@tool(mcp)
def link(ffe: str, zig: str, relation: str, note: str) -> dict:
    """Propose how an FFE piece (``FUNC_n``, ``DATA_n``, ``seg:N`` or a label path) meets the
    Zig engine (``path`` or ``path:symbol``, existing or planned). ``relation``: replaces,
    wraps, stubs, calls_into, reads or mirrors. ``note``: why, and what has to change on
    either side. A design proposal for the human, not a claim about FFE: it moves no beliefs."""
    return _refusing(br.link, _ledger, ffe, zig, relation, note, SOURCE, _zig)


@tool(mcp)
def claim(func: str, label: str, confidence: float, basis: str) -> dict:
    """Claim ``func`` is ``label`` (e.g. ``render/texture/palette``) with your honest
    probability that the full path is right. ``basis``: the static facts it rests on.
    One open claim per function; it stands until an experiment settles it."""
    try:
        return rs.claim(_ledger, rs.func_num(func), label, confidence, SOURCE, basis)
    except (ValueError, KeyError) as e:                 # a refusal is an answer, not a server error
        return {"refused": str(e)}


if __name__ == "__main__":
    mcp.run()
