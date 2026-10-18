"""The red team: someone whose job is to prove the claims wrong.

Three mechanisms:

1. **Attack targets.** Open high-confidence claims ranked by how much damage a
   wrong one would do: confidence x influence (how many functions depend on it),
   divided by how often it has already been tested. Each target comes with the
   strongest rival label and the scenario that best separates claim from rival.
2. **Attacks are bets.** The adversary places a rival-label bet plus a
   machine-checkable prediction (hit/miss in a scenario). Experiments resolve
   them; the adversary is Brier-scored like everyone else, so it can't win by
   contradicting everything.
3. **Blind second opinions.** ``blind_view`` shows a function's code and
   structure with no beliefs, no claims and (by default) no inherited names. A
   blind label is recorded as a claim; ``reveal`` compares it with other claims
   and queues a deciding experiment on disagreement.
"""
from __future__ import annotations

import json
import math
import re

from . import labels as lbl
from .halt import RULES
from .ledger import Ledger
from .loop import hit_model
from .models import AttackInput, AttackResult, BlindLabelInput

NAME_SUFFIX_RE = re.compile(r"\b(_?FUNC_\d+)_\w+")
NAMED_DATA_RE = re.compile(r"\b_DATA_[A-Za-z]\w*")


# ---------------------------------------------------------------- influence
def influence(ledger: Ledger, func: int) -> int:
    """Functions that would inherit a wrong label: callers within two hops plus callees."""
    con = ledger.con
    callers1 = {r["caller"] for r in con.execute("SELECT caller FROM calls WHERE callee=?", (func,))}
    callers2 = set()
    for c in callers1:
        callers2 |= {r["caller"] for r in con.execute("SELECT caller FROM calls WHERE callee=?", (c,))}
    callees = {r["callee"] for r in con.execute("SELECT callee FROM calls WHERE caller=?", (func,))}
    return len((callers1 | callers2 | callees) - {func})


def times_tested(ledger: Ledger, func: int) -> int:
    return ledger.con.execute(
        "SELECT COUNT(*) AS n FROM evidence WHERE func=? AND experiment_id IS NOT NULL", (func,)).fetchone()["n"]


# ---------------------------------------------------------------- test design
def design_test(claim: str, rival: str, labels: list[str]) -> dict:
    """Pick the scenario context that best separates ``claim`` from ``rival``."""
    best = None
    for tag in RULES["context"]:
        model = hit_model([tag], labels)["hit"]
        diff = model[claim] - model[rival]
        if best is None or abs(diff) > abs(best["separation"]):
            best = {"context": tag, "p_hit_if_claim": round(model[claim], 3),
                    "p_hit_if_rival": round(model[rival], 3), "separation": round(diff, 3)}
    if best["separation"] >= 0:
        best["claim_predicts"], best["rival_predicts"] = "hit", "miss"
    else:
        best["claim_predicts"], best["rival_predicts"] = "miss", "hit"
    return best


def strongest_rival(ledger: Ledger, func: int, claimed: str) -> str:
    """Rival favoured by experiment evidence; with none yet, the most testable alternative."""
    others = [lab for lab in ledger.labels if lab not in (claimed, "unknown")]
    dist, n = ledger.experimental_belief(func)
    if n:
        return max(others, key=lambda lab: dist[lab])
    return max(others, key=lambda lab: abs(design_test(claimed, lab, ledger.labels)["separation"]))


def attack_targets(ledger: Ledger, limit: int = 10, min_conf: float = 0.6) -> list[dict]:
    rows = ledger.con.execute(
        "SELECT * FROM bets WHERE outcome IS NULL AND kind='claim' AND prob>=? "
        "AND source NOT LIKE 'adversary%'", (min_conf,)).fetchall()
    out = []
    for bet in rows:
        f, claimed = bet["func"], lbl.top(bet["label"])
        infl, tested = influence(ledger, f), times_tested(ledger, f)
        attacked = ledger.con.execute(
            "SELECT COUNT(*) AS n FROM bets WHERE parent_id=? AND outcome IS NULL", (bet["id"],)).fetchone()["n"]
        score = bet["prob"] * (1 + math.log1p(infl)) / (1 + tested) / (1 + attacked)
        rival = strongest_rival(ledger, f, claimed)
        out.append({"claim_bet": bet["id"], "func": f, "claimant": bet["source"], "claimed": claimed,
                    "claimed_path": bet["label"],
                    "confidence": bet["prob"], "influence": infl, "times_tested": tested,
                    "open_attacks": attacked, "priority": round(score, 3), "rival": rival,
                    "test": design_test(claimed, rival, ledger.labels)})
    return sorted(out, key=lambda d: -d["priority"])[:limit]


def place_attack(ledger: Ledger, claim_bet: int, scenario: str, rival: str,
                 expect: str, prob: float, source: str = "adversary") -> dict:
    """Bet against a claim: a rival-label bet plus a checkable prediction in ``scenario``.

    ``expect`` is what the *rival* hypothesis predicts ("hit" or "miss");
    ``prob`` is the adversary's honest probability that this happens.
    """
    AttackInput(claim_bet=claim_bet, scenario=scenario, rival=rival, expect=expect, prob=prob, source=source)
    bet = ledger.con.execute("SELECT * FROM bets WHERE id=?", (claim_bet,)).fetchone()
    if bet is None or bet["kind"] != "claim":
        raise ValueError(f"bet {claim_bet} is not a claim")
    f = bet["func"]
    rival = lbl.validate(rival, ledger.labels)
    if lbl.top(rival) == lbl.top(bet["label"]):
        raise ValueError("a rival must differ from the claim at the top level; "
                         "finer-grained disagreements are settled by knockouts")
    lbl.register(ledger.con, rival, source)
    rival_bet = ledger.place_bet(source, f, f"label={rival}", f"{f} is {rival}, not {bet['label']}",
                                 prob, bet["domain"], kind="rival", label=rival, parent_id=claim_bet)
    pred_bet = ledger.place_bet(source, f, f"{scenario}:{expect}",
                                f"FUNC {f} will {expect} in {scenario}", prob, bet["domain"],
                                spec={"scenario": scenario, "func": f, "expect": expect},
                                parent_id=claim_bet)
    return AttackResult(rival_bet=rival_bet, prediction_bet=pred_bet,
                        run={"tool": "run_logging_pass", "scenario": scenario, "funcs": [f]}).model_dump()


def adversary_report(ledger: Ledger, source: str = "adversary") -> dict:
    con = ledger.con
    attacked_claims = con.execute(
        "SELECT DISTINCT parent_id FROM bets WHERE source=? AND parent_id IS NOT NULL", (source,)).fetchall()
    ids = [r["parent_id"] for r in attacked_claims]
    refuted = upheld = 0
    for i in ids:
        c = con.execute("SELECT outcome FROM bets WHERE id=?", (i,)).fetchone()
        if c and c["outcome"] is not None:
            refuted += c["outcome"] == 0
            upheld += c["outcome"] == 1
    return {"claims_attacked": len(ids), "claims_refuted": refuted, "claims_upheld": upheld,
            "calibration": ledger.calibration(source)}


# ---------------------------------------------------------------- blind opinions
def blind_view(ledger: Ledger, func: int, hide_inherited: bool = True, max_lines: int = 150) -> dict:
    """Code and structure only: no beliefs, no claims, no names unless asked."""
    con = ledger.con
    row = con.execute("SELECT * FROM functions WHERE num=?", (func,)).fetchone()
    if row is None:
        raise KeyError(func)
    asm = row["asm_text"] or ""
    if hide_inherited:
        asm = NAMED_DATA_RE.sub("DATA_named", NAME_SUFFIX_RE.sub(r"\1", asm))
    lines = asm.splitlines()
    seg = None
    if row["ord"] is not None:
        s = con.execute("SELECT seg, first_ord, last_ord, anchors FROM segments WHERE first_ord<=? AND last_ord>=?",
                        (row["ord"], row["ord"])).fetchone()
        if s:
            seg = {"seg": s["seg"], "size": s["last_ord"] - s["first_ord"] + 1,
                   "position_in_segment": row["ord"] - s["first_ord"],
                   "anchor_tables": json.loads(s["anchors"] or "[]")}
    return {
        "func": func, "pos": hex(row["pos"]) if row["pos"] is not None else None,
        "instructions": row["n_ins"], "has_frame": bool(row["has_frame"]),
        "aligned4": None if row["aligned4"] is None else bool(row["aligned4"]),
        "callers": sorted({r["caller"] for r in con.execute("SELECT caller FROM calls WHERE callee=?", (func,))}),
        "callees": sorted({r["callee"] for r in con.execute("SELECT callee FROM calls WHERE caller=?", (func,))}),
        "data_refs": sorted({r["data"] for r in con.execute("SELECT data FROM data_refs WHERE func=?", (func,))}),
        "segment": seg,
        "asm": "\n".join(lines[:max_lines]) + (f"\n... ({len(lines) - max_lines} more lines)" if len(lines) > max_lines else ""),
        "inherited_name": None if hide_inherited else row["name"],
    }


def record_blind(ledger: Ledger, func: int, label: str, confidence: float, source: str) -> int:
    """Store a blind label. It also enters the ledger as an ordinary claim (an opinion)."""
    BlindLabelInput(func=func, label=label, confidence=confidence, source=source)
    label = lbl.validate(label, ledger.labels)
    blind_id = ledger.con.execute(
        "INSERT INTO blind_labels (func, source, label, confidence) VALUES (?,?,?,?) RETURNING id",
        (func, source, label, confidence)).fetchone()["id"]
    ledger.con.commit()
    ledger.call(func, label, confidence, source=source, detail="blind")
    return int(blind_id)


def reveal(ledger: Ledger, func: int) -> list[dict]:
    """Compare blind labels with every other claim on ``func``; queue tests on disagreement."""
    con = ledger.con
    out = []
    claims = con.execute("SELECT * FROM bets WHERE func=? AND kind='claim'", (func,)).fetchall()
    for b in con.execute("SELECT * FROM blind_labels WHERE func=? AND revealed=0", (func,)).fetchall():
        others = [c for c in claims if c["source"] != b["source"]]
        disagree = [c for c in others if lbl.top(c["label"]) != lbl.top(b["label"])]
        agrees = None if not others else int(not disagree)   # None: nothing to compare yet
        con.execute("UPDATE blind_labels SET revealed=1, agrees=? WHERE id=?", (agrees, b["id"]))
        item = {"blind_id": b["id"], "func": func, "blind_source": b["source"], "blind_label": b["label"],
                "agrees": None if agrees is None else bool(agrees), "other_claims": [(c["source"], c["label"], c["prob"]) for c in others]}
        if disagree:
            c = max(disagree, key=lambda c: c["prob"])
            item["deciding_test"] = design_test(lbl.top(c["label"]), lbl.top(b["label"]), ledger.labels)
        elif any(c["label"] != b["label"] for c in others):
            item["path_differs"] = True              # same subsystem, different detail: a knockout decides
        out.append(item)
    con.commit()
    return out


def disagreements(ledger: Ledger) -> list[dict]:
    rows = ledger.con.execute(
        "SELECT bl.* FROM blind_labels bl WHERE bl.revealed=1 AND bl.agrees=0").fetchall()
    out = []
    for r in rows:
        open_claims = ledger.con.execute(
            "SELECT COUNT(*) AS n FROM bets WHERE func=? AND kind='claim' AND outcome IS NULL",
            (r["func"],)).fetchone()["n"]
        if open_claims:
            out.append(dict(r))
    return out
