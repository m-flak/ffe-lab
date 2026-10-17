"""One cycle of the agent loop: choose an experiment, run it, score, update.

The LLM agent sits above this (via the MCP server): it reads the uncertainty
map, adds its own guesses and bets, and asks you questions. These functions
are the mechanical part it calls so every experiment is recorded and scored
the same way. Each run is stored as an experiment, and only experiments can
resolve bets.
"""
from __future__ import annotations

from . import behavior
from .halt import RULES, HaltSnapshot, score_halt, score_never_hit
from .harness.backend import GameBackend
from .ledger import Ledger


def hit_model(context: list[str], labels: list[str], base: float = 0.3) -> dict[str, dict[str, float]]:
    """Outcome model for a logging pass: P(hit | label) in a scenario with these tags."""
    p_hit = {}
    for lab in labels:
        boost = 1.0
        for tag in context:
            boost *= RULES["context"].get(tag, {}).get(lab, 1.0)
        p_hit[lab] = min(0.95, base * boost)
    return {"hit": p_hit, "miss": {lab: 1 - p for lab, p in p_hit.items()}}


def rank_targets(ledger: Ledger, candidates: list[int], context: list[str], k: int = 50) -> list[tuple[int, float]]:
    """Candidates sorted by expected information gain of a logging breakpoint here."""
    model = hit_model(context, ledger.labels)
    scored = [(f, ledger.expected_info_gain(f, model)) for f in candidates]
    return sorted(scored, key=lambda t: -t[1])[:k]


def logging_pass(backend: GameBackend, ledger: Ledger, scenario: str, funcs: list[int],
                 context: list[str], keys: list[str] | None = None, frames: int = 100,
                 source: str = "halt") -> dict:
    """Load a scenario, log-break on ``funcs``, play ``keys``, update beliefs from hit counts.

    Returns {"experiment": id, "counts": {...}, "resolved": [...], "settled": [...]}."""
    backend.clear_breakpoints()
    backend.load_state(scenario)
    backend.set_breakpoints(funcs, log_only=True)
    if keys:
        backend.press_keys(keys)
    backend.wait(1000)
    counts = backend.hit_counts()
    backend.clear_breakpoints()
    exp = ledger.record_experiment("logging_pass", scenario,
                                   {"funcs": funcs, "keys": keys, "frames": frames,
                                    "context": context, "counts": counts})
    for f in funcs:
        n = counts.get(f, 0)
        if n:
            snap = HaltSnapshot(func=f, scenario=scenario, hits=n, frames=frames, context=context,
                                trigger=keys[-1] if keys else None)
            ledger.update(f, score_halt(snap, labels=ledger.labels), source, "log_hit",
                          f"{scenario}: {n} hits / {frames} frames", strength=0.8, experiment_id=exp)
        else:
            ledger.update(f, score_never_hit(context, ledger.labels), source, "log_miss",
                          f"{scenario}: no hits", strength=0.8, experiment_id=exp)
    full_counts = {f: counts.get(f, 0) for f in funcs}
    for f, n in full_counts.items():
        behavior.record(ledger, f, scenario, n, frames, exp)
    ledger.con.commit()
    return {"experiment": exp, "counts": full_counts,
            "resolved": ledger.auto_resolve(exp, scenario, full_counts, frames),
            "settled": ledger.settle_claims()}


def halt_and_score(backend: GameBackend, ledger: Ledger, scenario: str, func: int,
                   context: list[str], module_label: dict[int, str] | None = None,
                   keys: list[str] | None = None, timeout_ms: int = 5000) -> HaltSnapshot | None:
    """Full halt on one function, capture the snapshot, update its belief."""
    backend.clear_breakpoints()
    backend.load_state(scenario)
    backend.set_breakpoints([func])
    if keys:
        backend.press_keys(keys)
    snap = backend.run_until_halt(timeout_ms, scenario)
    backend.clear_breakpoints()
    if snap is None:
        exp = ledger.record_experiment("halt", scenario, {"func": func, "keys": keys, "halted": False})
        ledger.update(func, score_never_hit(context, ledger.labels), "halt", "halt_timeout",
                      scenario, experiment_id=exp)
        ledger.auto_resolve(exp, scenario, {func: 0}, 1)
        ledger.settle_claims()
        return None
    snap.context = snap.context or context
    exp = ledger.record_experiment("halt", scenario, {"func": func, "keys": keys, "halted": True,
                                                       "snapshot": snap.to_json()})
    ledger.update(func, score_halt(snap, module_label, ledger.labels), "halt", "halt",
                  snap.to_json(), experiment_id=exp)
    ledger.auto_resolve(exp, scenario, {func: max(snap.hits, 1)}, max(snap.frames, 1))
    ledger.settle_claims()
    return snap
