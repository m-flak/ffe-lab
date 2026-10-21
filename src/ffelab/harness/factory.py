"""Build a game backend from a kind name, shared by the MCP server and the orchestrator CLI.

``fake``: the deterministic stand-in with every registered scenario and no
hits (dry runs). ``qemu`` / ``dosbox-x``: the real game, configured by the
FFELAB_EMU JSON (see harness/emulator.py); its optional ``binary`` is the id
from ``ffelab exe-check`` and selects that build's address map.
"""
from __future__ import annotations

import json
import os
import sqlite3

from .backend import GameBackend


def make_backend(kind: str, con: sqlite3.Connection, emu: dict | None = None) -> GameBackend:
    if kind == "fake":
        from .fake import FakeBackend
        names = [r["name"] for r in con.execute("SELECT name FROM scenarios")] or ["demo"]
        return FakeBackend({n: {} for n in names})
    from ..exe import func_sizes, load_map
    from .emulator import EmulatorBackend, EmulatorConfig
    raw = dict(emu if emu is not None else json.loads(os.environ.get("FFELAB_EMU", "{}")))
    binary = raw.pop("binary", None)
    cfg = EmulatorConfig(**raw | {"kind": kind})
    pos = (load_map(con, binary, min_conf=0.7) if binary else
           {r["num"]: r["pos"] for r in con.execute("SELECT num,pos FROM functions WHERE pos IS NOT NULL")})
    return EmulatorBackend(cfg, pos, func_sizes(con))
