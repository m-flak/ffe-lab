"""MCP server: the tool surface an AI agent uses to play, break and learn.

Run:  FFELAB_DB=ffe.db FFELAB_BACKEND=fake python -m ffelab.mcp_server
      (FFELAB_DB may be a postgresql:// URL, so several agents can share one ledger)
Point Claude Code (or any MCP client) at it. Backends: ``fake`` for dry runs,
``qemu`` / ``dosbox-x`` for the real game (see harness/emulator.py).
"""
from __future__ import annotations

import os

try:                                   # mcp >= 2
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:                    # mcp 1.x
    from mcp.server.fastmcp import FastMCP

from . import adversary as adv
from . import behavior
from . import fe2ref
from . import knockout as ko
from . import labels as lbl
from . import observe
from . import propagate as prop
from . import db as dbmod
from .ledger import Ledger
from .loop import halt_and_score, logging_pass, rank_targets
from .mcp_thread import on_db_thread, tool

mcp = FastMCP("ffelab")
_con = on_db_thread(dbmod.connect, os.environ.get("FFELAB_DB", "ffe.db"))   # used only on the tool thread
_ledger = Ledger(_con)
_backend = None


def backend():
    global _backend
    if _backend is None:
        from .harness.factory import make_backend
        _backend = make_backend(os.environ.get("FFELAB_BACKEND", "fake"), _con)
    return _backend


# ---------------------------------------------------------------- game control
@tool(mcp)
def load_state(name: str) -> str:
    """Restore an emulator save state (a ready-made game situation)."""
    backend().load_state(name); return f"loaded {name}"


@tool(mcp)
def press_keys(keys: list[str]) -> str:
    """Send key presses to the game, e.g. ["F1", "F1"] or ["Esc"]."""
    backend().press_keys(keys); return f"pressed {keys}"


@tool(mcp)
def screenshot(path: str = "shot.png") -> str:
    """Capture the game screen to a file; read it to see what's happening."""
    return backend().screenshot(path)


# ---------------------------------------------------------------- experiments
@tool(mcp)
def rank_breakpoints(context: list[str], candidates: list[int] | None = None, k: int = 30) -> list:
    """Functions where a logging breakpoint in a scenario with these tags is most informative."""
    cands = candidates or [f for f, _ in _ledger.most_uncertain(400)]
    return rank_targets(_ledger, cands, context, k)


def _ctx(scenario: str, context: list[str] | None) -> list[str]:
    return context if context is not None else sorted(behavior.scenarios(_ledger).get(scenario, set()))


@tool(mcp)
def run_logging_pass(scenario: str, funcs: list[int], context: list[str] | None = None,
                     keys: list[str] | None = None) -> dict:
    """Count hits on many functions in one scenario; updates beliefs from hits and misses,
    records an experiment, auto-resolves bets it answers and settles decisive claims."""
    return logging_pass(backend(), _ledger, scenario, funcs, _ctx(scenario, context), keys)


@tool(mcp)
def run_halt(scenario: str, func: int, context: list[str] | None = None, keys: list[str] | None = None) -> str:
    """Freeze the game at one function, capture stack/args/regs, update its belief."""
    snap = halt_and_score(backend(), _ledger, scenario, func, _ctx(scenario, context), keys=keys)
    return snap.to_json() if snap else "no halt (function did not run)"


# ---------------------------------------------------------------- ledger
@tool(mcp)
def belief(func: int) -> dict:
    """Current label probabilities for a function, its experiment-only belief, open claims
    and static facts. (Red-team and blind agents should use blind_view instead.)"""
    row = _con.execute("SELECT * FROM functions WHERE num=?", (func,)).fetchone()
    fn = dict(row) if row else None
    if fn:
        fn.pop("asm_text", None)
    exp, n = _ledger.experimental_belief(func)
    return {"function": fn, "belief": _ledger.belief(func),
            "experimental_belief": exp, "experiment_rows": n,
            "open_bets": [dict(b) for b in _ledger.open_bets(func)],
            "ref_matches": fe2ref.matches_for(_con, func)}   # other people's names: never in blind_view


@tool(mcp)
def place_bet(source: str, claim: str, prediction: str, prob: float, func: int | None = None,
              domain: str | None = None, scenario: str | None = None, expect: str | None = None) -> int:
    """Record a falsifiable prediction BEFORE running the experiment that tests it.
    Give scenario + expect ("hit"/"miss") to make it auto-resolve when that run happens."""
    spec = {"scenario": scenario, "func": func, "expect": expect} if scenario and expect and func else None
    return _ledger.place_bet(source, func, claim, prediction, prob, domain, spec=spec)


@tool(mcp)
def resolve_bet(bet_id: int, outcome: bool, experiment_id: int) -> float:
    """Resolve a bet by citing the experiment (from a run_* tool) that decided it.
    Rejected if the experiment predates the bet or didn't test the bet's function."""
    return _ledger.resolve_bet(bet_id, outcome, experiment_id)


@tool(mcp)
def settle_claims() -> list:
    """Resolve label claims where experiment-only evidence is now decisive."""
    return _ledger.settle_claims()


@tool(mcp)
def contradictions() -> list:
    """Open contradictions: experiments disagree with a confident claim. Review these first."""
    return _ledger.contradictions()


@tool(mcp)
def close_contradiction(contradiction_id: int, status: str) -> str:
    """Close a contradiction as 'explained' (experiment was flawed) or 'claim_withdrawn'."""
    if status not in ("explained", "claim_withdrawn"):
        return "status must be 'explained' or 'claim_withdrawn'"
    _ledger.close_contradiction(contradiction_id, status); return "closed"


# ---------------------------------------------------------------- red team
@tool(mcp)
def red_team_targets(limit: int = 10) -> list:
    """Claims most worth attacking: confident, influential, under-tested. Each comes with
    the strongest rival label and the scenario context that best separates them."""
    return adv.attack_targets(_ledger, limit)


@tool(mcp)
def place_attack(claim_bet: int, scenario: str, rival: str, expect: str, prob: float,
                 source: str = "adversary") -> dict:
    """Bet against a claim: rival label + what the rival predicts ('hit'/'miss') in a
    real save-state scenario, with your honest probability. Then run that scenario."""
    return adv.place_attack(_ledger, claim_bet, scenario, rival, expect, prob, source)


@tool(mcp)
def adversary_report(source: str = "adversary") -> dict:
    """How many claims the red team attacked, refuted and upheld, plus its calibration."""
    return adv.adversary_report(_ledger, source)


@tool(mcp)
def blind_view(func: int) -> dict:
    """A function's code and structure with no beliefs, claims or inherited names."""
    return adv.blind_view(_ledger, func)


@tool(mcp)
def similar_functions(func: int, k: int = 10, model: str = "asm-ngram-v1") -> list:
    """Functions most like ``func``: by instruction patterns (model asm-ngram-v1), by
    position in the call graph (callgraph-spectral-v1: functions that call each other or
    share partners) or by the scenarios they fire in (fp-scenario-v1). Numbers and cosine scores only, no
    names, labels or beliefs; open a match with blind_view. Similarity is a hint
    for where to look, never evidence."""
    from . import embed
    return embed.similar(_con, func, model, k)


@tool(mcp)
def record_blind_label(func: int, label: str, confidence: float, source: str) -> int:
    """Record a label made from blind_view only. Use a distinct source, e.g. 'ai:blind'."""
    return adv.record_blind(_ledger, func, label, confidence, source)


@tool(mcp)
def reveal_blind(func: int) -> list:
    """Compare blind labels with other claims; disagreements come with a deciding test."""
    return adv.reveal(_ledger, func)


@tool(mcp)
def record_call(func: int, label: str, confidence: float, source: str = "human", domain: str | None = None) -> dict:
    """An expert (the human, or the AI as 'ai:<model>') asserts a label with a confidence."""
    return _ledger.call(func, label, confidence, source, domain)


@tool(mcp)
def calibration(source: str) -> dict:
    """Calibration of a source: Brier, overconfidence (said vs. was right), a confidence
    curve and reliability per domain. Try source='human' to audit yourself."""
    return _ledger.calibration(source)


@tool(mcp)
def segment_of(func: int) -> dict | None:
    """Which recovered source-file segment a function belongs to."""
    row = _con.execute("SELECT ord FROM functions WHERE num=?", (func,)).fetchone()
    if not row or row["ord"] is None:
        return None
    seg = _con.execute("SELECT * FROM segments WHERE first_ord<=? AND last_ord>=?", (row["ord"], row["ord"])).fetchone()
    return dict(seg) if seg else None


# ---------------------------------------------------------------- unknowns: behaviour, knockouts, labels
@tool(mcp)
def register_scenario(name: str, tags: list[str], notes: str = "") -> str:
    """Describe a save state with tags (e.g. near_planet, atmosphere, altitude_low, docked).
    Scenarios differing by one tag become controlled experiments."""
    behavior.register_scenario(_ledger, name, tags, notes); return f"registered {name}"


@tool(mcp)
def list_scenarios() -> dict:
    """All registered save-state scenarios and their tags."""
    return {k: sorted(v) for k, v in behavior.scenarios(_ledger).items()}


@tool(mcp)
def fingerprint(func: int) -> dict:
    """What a function does without knowing what it is: activity state per scenario,
    which single tags flip it on/off, and the next contrast runs worth doing."""
    return {"profile": behavior.profile(_ledger, func),
            "tag_effects": behavior.tag_effects(_ledger, func),
            "suggested_contrasts": behavior.suggest_contrasts(_ledger, func)}


@tool(mcp)
def similar_behaviour(func: int, k: int = 10) -> list:
    """Functions whose behavioural profile matches this one's. (Renamed from a second
    `similar_functions`, which clashed with the embedding search tool of that name.)"""
    return behavior.similar(_ledger, func, k)


@tool(mcp)
def behaviour_groups(threshold: float = 0.85) -> dict:
    """Clusters of functions that behave alike: subsystems discovered before they're named."""
    return behavior.behaviour_groups(_ledger, threshold)


@tool(mcp)
def graph_votes(k: int = 10) -> dict:
    """Re-derive static label evidence from call-graph structure: the labels of each
    function's nearest neighbours in the call-graph basis, and which labels its labelled
    callers and callees allow (the quotient). Idempotent; never settles claims.
    Run `ffelab graph build` first."""
    from . import graph
    return graph.vote_all(_ledger, k)


@tool(mcp)
def quotient_outliers() -> list:
    """Calls between two labels that no other labelled call supports: each one is a
    mislabel or a real cross-layer call. Candidates for review, not verdicts."""
    from . import graph
    return graph.outliers(_ledger)


@tool(mcp)
def propagate_labels() -> dict:
    """Re-derive label evidence for every caller from its callees' labels (and inherited
    name families, weakly). Idempotent; never settles claims."""
    return prop.propagate_all(_ledger)


@tool(mcp)
def run_knockout(scenario: str, func: int, keys: list[str] | None = None, settle_ms: int = 1500) -> dict:
    """Disable a function (patch entry to RET) and compare screenshots against two baselines.
    Returns health (ok/crashed/frozen), changed-pixel fraction, bbox and image paths.
    If the outcome is 'visible', look at the diff image, then record_knockout_observation.
    On QEMU it also counts sound-card port writes: audio_outcome is audible / inaudible /
    no_audio (nothing playing in the baselines, so no verdict). For music, pass keys that
    start or change it inside settle_ms (launch, dock, combat)."""
    return ko.knockout(backend(), _ledger, scenario, func, keys, settle_ms,
                       os.environ.get("FFELAB_KNOCKOUT_DIR", "knockouts"))


@tool(mcp)
def run_watch_pass(scenario: str, watches: list[dict], keys: list[str] | None = None,
                   ms: int = 2000) -> dict:
    """Memory watchpoints: which functions write/read these addresses while the game runs.
    Each watch: {"addr": runtime address, "length": bytes, "kind": write|read|access,
    "region": optional halt region name such as "sample_data" (only then do beliefs move)}.
    Stops inside unmapped code (sound driver, DOS/4GW) are attributed to the nearest
    mapped caller on the stack."""
    from .models import Watch
    return observe.watch_pass(backend(), _ledger, scenario, [Watch(**w) for w in watches], keys, ms)


@tool(mcp)
def run_file_access_pass(scenario: str, exe_path: str | None = None, keys: list[str] | None = None,
                         ms: int = 3000) -> dict:
    """Which FFE functions open/read which files: breakpoints on the exe's own int 21h /
    int 31h sites decode DOS file calls. exe_path defaults to $FFELAB_EXE; the build's
    address map comes from FFELAB_EMU's "binary". Records an experiment; moves no beliefs."""
    import json
    path = exe_path or os.environ.get("FFELAB_EXE")
    if not path:
        raise ValueError("give exe_path or set FFELAB_EXE to the FIRSTENC.EXE running in the emulator")
    binary = json.loads(os.environ.get("FFELAB_EMU", "{}")).get("binary")
    sites = observe.file_sites(_ledger, path, binary)
    return observe.file_access_pass(backend(), _ledger, scenario, sites, keys, ms)


@tool(mcp)
def record_knockout_observation(experiment_id: int, func: int, path: str, confidence: float,
                                description: str, source: str) -> dict:
    """What vanished or changed in a knockout, as a label path (e.g.
    render/planet/atmosphere/clouds) with a one-line description. Creates the path if new."""
    return ko.record_observation(_ledger, experiment_id, func, path, confidence, description, source)


@tool(mcp)
def vocabulary(under: str | None = None) -> list:
    """The label tree discovered so far, with how many functions carry each path."""
    return lbl.vocabulary(_con, under)


@tool(mcp)
def function_labels(func: int) -> dict:
    """Every label path recorded for a function, and the most specific trusted one."""
    rows = [dict(r) for r in _con.execute("SELECT * FROM func_labels WHERE func=? ORDER BY id", (func,))]
    return {"labels": rows, "deepest": lbl.deepest_label(_con, func)}


if __name__ == "__main__":
    mcp.run()
