"""Behavioural fingerprints: what a function does, before anyone knows what it is.

Every logging pass records, per function and scenario, how often it fired. A
function's *profile* is its activity state in each scenario it has been
tested in:

    silent      never fired
    rare        fired, but well under once per frame (events, one-shots)
    regular     fired often, but not every frame
    per_frame   fired about once (or more) per frame

Functions with matching profiles cluster into *behaviour groups*, which are
subsystems discovered before they are named ("per-frame near atmospheric
planets, silent docked and in deep space"). Scenarios carry tags, so pairs of
scenarios that differ by exactly one tag act as controlled experiments: if a
function's state flips with the 'atmosphere' tag and nothing else, that tag
is what it responds to.
"""
from __future__ import annotations

import json
from collections import defaultdict
from itertools import combinations

from .ledger import Ledger

STATES = ("silent", "rare", "regular", "per_frame")


def state_of(rate: float, hits: int) -> str:
    if hits == 0:
        return "silent"
    if rate >= 0.9:
        return "per_frame"
    if rate >= 0.1:
        return "regular"
    return "rare"


# ---------------------------------------------------------------- scenarios
def register_scenario(ledger: Ledger, name: str, tags: list[str], notes: str = "") -> None:
    """Describe a save state with tags, e.g. ["near_planet", "atmosphere", "altitude_low"]."""
    ledger.con.execute("INSERT OR REPLACE INTO scenarios (name, tags, notes) VALUES (?,?,?)",
                       (name, json.dumps(sorted(set(tags))), notes))
    ledger.con.commit()


def scenarios(ledger: Ledger) -> dict[str, set[str]]:
    return {r["name"]: set(json.loads(r["tags"] or "[]"))
            for r in ledger.con.execute("SELECT * FROM scenarios")}


# ---------------------------------------------------------------- fingerprints
def record(ledger: Ledger, func: int, scenario: str, hits: int, frames: int, experiment_id: int | None) -> None:
    ledger.con.execute(
        "INSERT INTO fingerprints (func, scenario, hits, frames, rate, experiment_id) VALUES (?,?,?,?,?,?)",
        (func, scenario, hits, frames, hits / max(frames, 1), experiment_id))


def profile(ledger: Ledger, func: int) -> dict[str, dict]:
    """Per scenario: pooled hits/frames across runs and the resulting state."""
    acc: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for r in ledger.con.execute("SELECT scenario, hits, frames FROM fingerprints WHERE func=?", (func,)):
        a = acc[r["scenario"]]
        a[0] += r["hits"]; a[1] += r["frames"]; a[2] += 1
    out = {}
    for sc, (h, fr, runs) in acc.items():
        rate = h / max(fr, 1)
        out[sc] = {"state": state_of(rate, h), "rate": round(rate, 4), "runs": runs}
    return out


def similarity(pa: dict, pb: dict, min_common: int = 2) -> float | None:
    common = set(pa) & set(pb)
    if len(common) < min_common:
        return None
    return sum(pa[s]["state"] == pb[s]["state"] for s in common) / len(common)


def _profiles(ledger: Ledger) -> dict[int, dict]:
    funcs = [r["func"] for r in ledger.con.execute("SELECT DISTINCT func FROM fingerprints")]
    return {f: profile(ledger, f) for f in funcs}


def similar(ledger: Ledger, func: int, k: int = 10, min_common: int = 2) -> list[dict]:
    profs = _profiles(ledger)
    me = profs.get(func)
    if not me:
        return []
    out = []
    for f, p in profs.items():
        if f == func or all(v["state"] == "silent" for v in p.values()):
            continue
        s = similarity(me, p, min_common)
        if s is not None:
            out.append({"func": f, "similarity": round(s, 3), "common_scenarios": len(set(me) & set(p))})
    return sorted(out, key=lambda d: (-d["similarity"], -d["common_scenarios"]))[:k]


def describe(prof: dict[str, str]) -> str:
    by_state = defaultdict(list)
    for sc, st in sorted(prof.items()):
        by_state[st].append(sc)
    parts = [f"{st.replace('_', '-')} in {', '.join(by_state[st])}" for st in STATES[::-1] if by_state[st]]
    return "; ".join(parts)


def behaviour_groups(ledger: Ledger, threshold: float = 0.85, min_common: int = 2,
                     min_size: int = 2) -> dict:
    """Cluster functions by profile (single-linkage over similarity >= threshold).

    Functions silent everywhere they were tested are listed separately as
    ``never_seen``: absence alone doesn't make them alike."""
    profs = _profiles(ledger)
    active = {f: p for f, p in profs.items() if any(v["state"] != "silent" for v in p.values())}
    never = sorted(f for f in profs if f not in active)
    parent = {f: f for f in active}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x

    for a, b in combinations(active, 2):
        s = similarity(active[a], active[b], min_common)
        if s is not None and s >= threshold:
            parent[find(a)] = find(b)
    clusters = defaultdict(list)
    for f in active:
        clusters[find(f)].append(f)
    groups = []
    for members in clusters.values():
        if len(members) < min_size:
            continue
        # consensus state per scenario among members that were tested there
        votes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for f in members:
            for sc, v in active[f].items():
                votes[sc][v["state"]] += 1
        consensus = {sc: max(c, key=c.get) for sc, c in votes.items()}
        groups.append({"members": sorted(members), "size": len(members),
                       "profile": consensus, "description": describe(consensus)})
    groups.sort(key=lambda g: -g["size"])
    for i, g in enumerate(groups):
        g["group"] = i
    return {"groups": groups, "never_seen": never}


# ---------------------------------------------------------------- controlled contrasts
def tag_effects(ledger: Ledger, func: int) -> list[dict]:
    """Scenario pairs differing by exactly one tag, both tested: did the state change?"""
    sc = scenarios(ledger)
    prof = profile(ledger, func)
    out = []
    for a, b in combinations(sorted(prof), 2):
        if a not in sc or b not in sc:
            continue
        diff = sc[a] ^ sc[b]
        if len(diff) != 1:
            continue
        tag = next(iter(diff))
        with_, without = (a, b) if tag in sc[a] else (b, a)
        out.append({"tag": tag, "with": with_, "without": without,
                    "state_with": prof[with_]["state"], "state_without": prof[without]["state"],
                    "responds": prof[with_]["state"] != prof[without]["state"]})
    return out


def suggest_contrasts(ledger: Ledger, func: int, limit: int = 10) -> list[dict]:
    """Untested scenarios that differ by one tag from a scenario this function was tested in,
    preferring partners of scenarios where it was active."""
    sc = scenarios(ledger)
    prof = profile(ledger, func)
    out = []
    for tested, info in prof.items():
        if tested not in sc:
            continue
        for other, tags in sc.items():
            if other in prof:
                continue
            diff = sc[tested] ^ tags
            if len(diff) == 1:
                out.append({"run": other, "pair_with": tested, "varies": next(iter(diff)),
                            "state_in_pair": info["state"],
                            "priority": 2 if info["state"] != "silent" else 1})
    return sorted(out, key=lambda d: -d["priority"])[:limit]
