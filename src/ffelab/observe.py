"""Observation beyond the screen: who touches memory, who opens which file.

Two experiments for subsystems a screenshot can't tell apart (music, file
loading, state no one draws). Both are recorded as experiments, so their
results are ledger-grade evidence (DECISIONS D3); the sound channel for
knockouts lives in ``knockout.py``.

* ``watch_pass``: memory watchpoints on runtime addresses. Every stop records
  the function that touched the memory (or, inside unmapped code such as the
  sound driver, the nearest mapped caller on the stack). A watch with a
  ``region`` name (``halt.RULES["region"]``, e.g. ``sample_data``) moves the
  touching functions' beliefs the way a halt's pointer regions do; without one
  the pass only records who touched what.
* ``file_access_pass``: breakpoints on the game's own ``int 21h`` and DPMI
  ``int 31h`` sites (found in the exe, outside the mapped functions, since JJ's
  asm replaced FFE's C runtime) decode DOS file calls: open/create with the
  filename, read/write with handle and size. It records which FFE function
  asked for which file; it doesn't move beliefs, because turning a filename
  into a subsystem label is a guess, not an observation.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from . import exe
from .halt import RULES
from .harness.backend import GameBackend
from .ledger import Ledger
from .models import Watch


def watch_pass(backend: GameBackend, ledger: Ledger, scenario: str, watches: list[Watch],
               keys: list[str] | None = None, ms: int = 2000, max_hits: int = 500,
               source: str = "halt") -> dict:
    backend.clear_breakpoints()
    backend.load_state(scenario)
    backend.set_watchpoints(watches)
    try:
        if keys:
            backend.press_keys(keys)
        hits = backend.watch_hits(ms, max_hits)
    finally:
        backend.clear_watchpoints()
    counts = Counter(h.func for h in hits if h.func is not None)
    unattributed = sum(h.func is None for h in hits)
    exp = ledger.record_experiment("watch", scenario, {
        "watches": [w.model_dump() for w in watches], "keys": keys, "ms": ms,
        "hits": [h.model_dump() for h in hits], "counts": dict(counts), "unattributed": unattributed})

    def watch_of(addr: int) -> Watch | None:
        return next((w for w in watches if w.addr <= addr < w.addr + w.length), None)

    regions: dict[int, set[str]] = {}
    for h in hits:
        w = watch_of(h.addr)
        if h.func is not None and w and w.region in RULES["region"]:
            regions.setdefault(h.func, set()).add(w.region)
    for f, regs in regions.items():
        like = {lab: 1.0 for lab in ledger.labels}
        for r in regs:
            for lab, k in RULES["region"][r].items():
                if lab in like:
                    like[lab] *= k
        ledger.update(f, like, source, "watch", f"{scenario}: touched {', '.join(sorted(regs))}"
                      f" ({counts[f]} hits)", strength=0.8, experiment_id=exp)
    ledger.con.commit()
    return {"experiment": exp, "hits": len(hits), "counts": dict(counts), "unattributed": unattributed,
            "updated": sorted(regions)}


def file_sites(ledger: Ledger, exe_path: str | Path, binary: str | None = None) -> dict[int, str]:
    """``int 21h`` / ``int 31h`` sites (Pos -> via) in this exe, outside mapped functions."""
    data = Path(exe_path).read_bytes()
    con = ledger.con
    pos = (exe.load_map(con, binary, min_conf=0.7) if binary else
           {r["num"]: r["pos"] for r in con.execute("SELECT num,pos FROM functions WHERE pos IS NOT NULL")})
    return exe.int_sites(data, pos, exe.func_sizes(con))


def file_access_pass(backend: GameBackend, ledger: Ledger, scenario: str, sites: dict[int, str],
                     keys: list[str] | None = None, ms: int = 3000, max_hits: int = 200) -> dict:
    if not sites:
        raise ValueError("no int 21h / int 31h sites: check the exe path and that it is the game's exe")
    backend.clear_breakpoints()
    backend.load_state(scenario)
    if keys:
        backend.press_keys(keys)
    calls = backend.file_accesses(ms, sites, max_hits)
    opened = sorted({(c.func, c.filename) for c in calls if c.filename}, key=str)
    exp = ledger.record_experiment("file_access", scenario, {
        "keys": keys, "ms": ms, "sites": len(sites), "calls": [c.model_dump() for c in calls]})
    return {"experiment": exp, "calls": len(calls),
            "files": [{"func": f, "filename": n} for f, n in opened],
            "by_func": dict(Counter(c.func for c in calls))}
