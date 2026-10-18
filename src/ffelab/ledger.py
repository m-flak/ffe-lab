"""The evidence ledger: beliefs, bets, calibration and source reliability.

Every function has a categorical belief over subsystem labels. Evidence
arrives as a likelihood per label (from halts, static features, AI guesses or
your own calls) and updates the belief with Bayes' rule. Every claim a source
makes is a *bet* with a probability; when it resolves it is Brier-scored, and
the source's reliability (per domain) is the shrunk average of those scores.

Rules that keep everyone honest, including you:

* **No self-grading.** A bet can only be resolved by linking an experiment
  record created after the bet. Opinions (calls) never resolve bets.
* **Claims settle on experimental evidence only.** A label claim is judged
  against the belief rebuilt from experiment evidence alone, excluding every
  call by anyone.
* **Contradictions are loud.** When experiment evidence turns against an open
  high-confidence claim, a contradiction is recorded instead of the two being
  quietly averaged.
"""
from __future__ import annotations

import json
import math
import sqlite3

from . import labels as lbl
from .models import TOP_LABELS, Bet, BetSpec, ClaimInput, Evidence, Experiment

DEFAULT_LABELS = list(TOP_LABELS)

# Prior reliability by source kind and how many pseudo-bets that prior is worth.
# Kept small so a run of misses pulls reliability down quickly.
PRIOR_RELIABILITY = {"human": 0.9, "ai": 0.6, "adversary": 0.6, "static": 0.6, "halt": 0.7}
PRIOR_WEIGHT = 3.0
FLOOR = 1e-4
OPINION_KINDS = {"call"}                 # evidence kinds that are opinions, not experiments
STATIC_KINDS = {"static_callee"}         # derived from code structure / inherited names, not runs
CONTRADICTION_RATIO = 0.25               # claimed label vs best rival, experiment evidence only
CONTRADICTION_MIN_CONF = 0.7


def entropy(dist: dict[str, float]) -> float:
    return -sum(p * math.log2(p) for p in dist.values() if p > 0)


class Ledger:
    def __init__(self, con: sqlite3.Connection, labels: list[str] | None = None):
        self.con = con
        self.labels = labels or DEFAULT_LABELS

    # ------------------------------------------------------------ beliefs
    def belief(self, func: int) -> dict[str, float]:
        rows = self.con.execute("SELECT label, prob FROM beliefs WHERE func=?", (func,)).fetchall()
        if not rows:
            u = 1.0 / len(self.labels)
            return {lab: u for lab in self.labels}
        d = {r["label"]: r["prob"] for r in rows}
        return {lab: d.get(lab, FLOOR) for lab in self.labels}

    def _store(self, func: int, dist: dict[str, float]) -> None:
        self.con.executemany("INSERT OR REPLACE INTO beliefs VALUES (?,?,?)",
                             [(func, lab, p) for lab, p in dist.items()])

    def set_prior(self, func: int, dist: dict[str, float]) -> None:
        self._store(func, _normalise({lab: dist.get(lab, FLOOR) for lab in self.labels}))
        self.con.commit()

    def update(self, func: int, likelihoods: dict[str, float], source: str,
               kind: str, detail: str = "", strength: float = 1.0,
               experiment_id: int | None = None) -> dict[str, float]:
        """Bayes update. ``strength`` in [0,1] tempers the likelihood (L ** strength)."""
        prior = self.belief(func)
        tempered = {lab: max(likelihoods.get(lab, 1.0), FLOOR) ** strength for lab in self.labels}
        post = _normalise({lab: prior[lab] * tempered[lab] for lab in self.labels})
        self._store(func, post)
        cur = self.con.execute(
            "INSERT INTO evidence (func, source, kind, likelihoods, detail, experiment_id)"
            " VALUES (?,?,?,?,?,?)",
            (func, source, kind, json.dumps(tempered), detail, experiment_id))
        self.con.commit()
        if kind not in OPINION_KINDS | STATIC_KINDS:
            self._check_contradictions(func, int(cur.lastrowid))
        return post

    def rebuild(self, func: int) -> dict[str, float]:
        """Recompute a belief from a uniform prior and every stored evidence row."""
        dist = {lab: 1.0 for lab in self.labels}
        for r in self.con.execute("SELECT likelihoods FROM evidence WHERE func=?", (func,)):
            like = json.loads(r["likelihoods"])
            for lab in self.labels:
                dist[lab] *= max(like.get(lab, 1.0), FLOOR)
        post = _normalise(dist)
        self._store(func, post)
        self.con.commit()
        return post

    def replace_evidence(self, func: int, kind: str, source: str, likelihoods: dict[str, float],
                         detail: str = "") -> dict[str, float]:
        """Swap out this function's evidence of ``kind`` (idempotent re-derivation, e.g.
        propagation re-run after neighbours change) and rebuild its belief."""
        self.con.execute("DELETE FROM evidence WHERE func=? AND kind=?", (func, kind))
        tempered = {lab: max(likelihoods.get(lab, 1.0), FLOOR) for lab in self.labels}
        self.con.execute("INSERT INTO evidence (func, source, kind, likelihoods, detail) VALUES (?,?,?,?,?)",
                         (func, source, kind, json.dumps(tempered), detail))
        return self.rebuild(func)

    def experimental_belief(self, func: int) -> tuple[dict[str, float], int]:
        """Belief rebuilt from a uniform prior using experiment evidence only
        (no calls, no static derivations). Returns (distribution, number of rows)."""
        dist = {lab: 1.0 for lab in self.labels}
        n = 0
        for r in self.con.execute("SELECT kind, likelihoods FROM evidence WHERE func=?", (func,)):
            if r["kind"] in OPINION_KINDS | STATIC_KINDS:
                continue
            like = json.loads(r["likelihoods"])
            for lab in self.labels:
                dist[lab] *= max(like.get(lab, 1.0), FLOOR)
            n += 1
        return _normalise(dist), n

    # ------------------------------------------------------------ expert calls
    def call(self, func: int, label: str, confidence: float, source: str = "human",
             domain: str | None = None, detail: str = "") -> dict[str, float]:
        """A source asserts ``func`` is ``label`` with ``confidence``.

        ``label`` may be a path (``render/planet/atmosphere/clouds``); the belief
        moves on its top-level part, the full path is kept in the vocabulary and on
        the function. Effective strength = confidence x reliability(source, domain).
        The call is recorded as an open *claim* bet, settled by experiment evidence only.
        """
        ClaimInput(func=func, label=label, confidence=confidence, source=source, domain=domain)
        path = lbl.validate(label, self.labels)
        label = lbl.top(path)
        lbl.register(self.con, path, source)
        self.con.execute("INSERT INTO func_labels (func, path, source, confidence, note) VALUES (?,?,?,?,?)",
                         (func, path, source, confidence, "claim"))
        r = self.reliability(source, domain)
        s = max(0.0, min(1.0, confidence * r))
        k = len(self.labels)
        like = {lab: (s + (1 - s) / k) if lab == label else (1 - s) / k for lab in self.labels}
        self.place_bet(source, func, f"label={path}", f"{func} is {path}", confidence, domain,
                       kind="claim", label=path)
        post = self.update(func, like, source, "call", detail or f"{path}@{confidence:.2f} r={r:.2f}")
        self._check_contradictions(func, None)        # experiments may already disagree
        return post

    # ------------------------------------------------------------ experiments
    def record_experiment(self, kind: str, scenario: str, detail: dict | None = None) -> int:
        cur = self.con.execute("INSERT INTO experiments (kind, scenario, detail) VALUES (?,?,?)",
                               (kind, scenario, json.dumps(detail or {})))
        self.con.commit()
        return int(cur.lastrowid)

    # ------------------------------------------------------------ bets
    def place_bet(self, source: str, func: int | None, claim: str, prediction: str,
                  prob: float, domain: str | None = None, kind: str = "prediction",
                  label: str | None = None, spec: dict | None = None,
                  parent_id: int | None = None) -> int:
        """Record a bet. ``spec`` makes it machine-checkable, e.g.
        {"scenario": "combat", "func": 688, "expect": "hit"}  (hit | miss)
        {"scenario": "docked", "func": 12, "min_rate": 0.9} / {"max_rate": 0.05}."""
        if not 0.0 <= prob <= 1.0:
            raise ValueError("prob must be in [0, 1]")
        if spec is not None:
            spec = BetSpec.model_validate(spec).model_dump(exclude_defaults=True)
        cur = self.con.execute(
            "INSERT INTO bets (source, domain, func, claim, prediction, prob, kind, label, spec, parent_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (source, domain, func, claim, prediction, prob, kind, label,
             json.dumps(spec) if spec else None, parent_id))
        self.con.commit()
        return int(cur.lastrowid)

    def resolve_bet(self, bet_id: int, outcome: bool, experiment_id: int) -> float:
        """Resolve a bet. Requires an experiment recorded *after* the bet was placed."""
        bet = self.con.execute("SELECT * FROM bets WHERE id=?", (bet_id,)).fetchone()
        if bet is None:
            raise KeyError(f"no bet {bet_id}")
        if bet["outcome"] is not None:
            raise ValueError(f"bet {bet_id} already resolved")
        exp = self.con.execute("SELECT * FROM experiments WHERE id=?", (experiment_id,)).fetchone()
        if exp is None:
            raise ValueError("bets can only be resolved by a recorded experiment")
        if exp["created"] < bet["created"]:
            raise ValueError("experiment predates the bet; it cannot resolve it")
        detail = json.loads(exp["detail"] or "{}")
        tested = set(detail.get("funcs") or []) | ({detail["func"]} if "func" in detail else set())
        if bet["func"] is not None and tested and bet["func"] not in tested:
            raise ValueError(f"experiment {experiment_id} did not test FUNC {bet['func']}")
        brier = (bet["prob"] - (1.0 if outcome else 0.0)) ** 2
        self.con.execute(
            "UPDATE bets SET outcome=?, brier=?, experiment_id=?, resolved=CURRENT_TIMESTAMP WHERE id=?",
            (int(outcome), brier, experiment_id, bet_id))
        self.con.commit()
        return brier

    def auto_resolve(self, experiment_id: int, scenario: str, counts: dict[int, int],
                     frames: int) -> list[dict]:
        """Resolve every open machine-checkable bet this experiment answers."""
        done = []
        for bet in self.con.execute("SELECT * FROM bets WHERE outcome IS NULL AND spec IS NOT NULL").fetchall():
            spec = json.loads(bet["spec"])
            if spec.get("scenario") != scenario or spec.get("func") not in counts:
                continue
            n = counts[spec["func"]]
            rate = n / max(frames, 1)
            if spec.get("expect") == "hit":
                outcome = n > 0
            elif spec.get("expect") == "miss":
                outcome = n == 0
            elif "min_rate" in spec:
                outcome = rate >= spec["min_rate"]
            elif "max_rate" in spec:
                outcome = rate <= spec["max_rate"]
            else:
                continue
            brier = self.resolve_bet(bet["id"], outcome, experiment_id)
            done.append({"bet": bet["id"], "source": bet["source"], "outcome": outcome, "brier": brier})
        return done

    def settle_claims(self, threshold: float = 0.95, min_evidence: int = 3) -> list[dict]:
        """Resolve label claims once experiment-only evidence is decisive either way."""
        done = []
        for bet in self.con.execute(
                "SELECT * FROM bets WHERE outcome IS NULL AND kind IN ('claim','rival')").fetchall():
            dist, n = self.experimental_belief(bet["func"])
            if n < min_evidence:
                continue
            top, p = max(dist.items(), key=lambda kv: kv[1])
            if p < threshold:
                continue
            exp = self.con.execute(
                "SELECT MAX(experiment_id) AS e FROM evidence WHERE func=? AND experiment_id IS NOT NULL",
                (bet["func"],)).fetchone()["e"]
            if exp is None:
                continue
            outcome = top == lbl.top(bet["label"])     # settles the top level; finer paths need knockouts
            brier = self.resolve_bet(bet["id"], outcome, exp)
            done.append({"bet": bet["id"], "source": bet["source"], "label": bet["label"],
                         "settled_as": top, "outcome": outcome, "brier": brier})
        return done

    def get_bet(self, bet_id: int) -> Bet:
        row = self.con.execute("SELECT * FROM bets WHERE id=?", (bet_id,)).fetchone()
        if row is None:
            raise KeyError(f"no bet {bet_id}")
        return Bet.from_row(row)

    def get_experiment(self, experiment_id: int) -> Experiment:
        row = self.con.execute("SELECT * FROM experiments WHERE id=?", (experiment_id,)).fetchone()
        if row is None:
            raise KeyError(f"no experiment {experiment_id}")
        return Experiment.from_row(row)

    def evidence(self, func: int) -> list[Evidence]:
        return [Evidence.from_row(r) for r in
                self.con.execute("SELECT * FROM evidence WHERE func=? ORDER BY id", (func,))]

    def open_bets(self, func: int | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM bets WHERE outcome IS NULL" + (" AND func=?" if func is not None else "")
        return self.con.execute(q, (() if func is None else (func,))).fetchall()

    # ------------------------------------------------------------ contradictions
    def _check_contradictions(self, func: int, evidence_id: int | None) -> None:
        dist, n = self.experimental_belief(func)
        if n == 0:
            return
        for bet in self.con.execute(
                "SELECT * FROM bets WHERE func=? AND outcome IS NULL AND kind='claim' AND prob>=?",
                (func, CONTRADICTION_MIN_CONF)).fetchall():
            claimed = lbl.top(bet["label"])
            rival, rp = max(((lab, p) for lab, p in dist.items() if lab != claimed), key=lambda kv: kv[1])
            ratio = dist.get(claimed, FLOOR) / max(rp, FLOOR)
            if ratio >= CONTRADICTION_RATIO:
                continue
            exists = self.con.execute(
                "SELECT 1 FROM contradictions WHERE bet_id=? AND status='open'", (bet["id"],)).fetchone()
            if exists:
                self.con.execute("UPDATE contradictions SET ratio=?, favoured=? WHERE bet_id=? AND status='open'",
                                 (ratio, rival, bet["id"]))
            else:
                self.con.execute(
                    "INSERT INTO contradictions (func, bet_id, evidence_id, claimed, favoured, ratio, detail)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (func, bet["id"], evidence_id, claimed, rival, ratio,
                     f"{bet['source']} claimed {claimed}@{bet['prob']:.2f}; experiments favour {rival} "
                     f"({n} rows, ratio {ratio:.3f})"))
        self.con.commit()

    def contradictions(self, status: str = "open") -> list[dict]:
        return [dict(r) for r in self.con.execute(
            "SELECT * FROM contradictions WHERE status=? ORDER BY ratio ASC", (status,))]

    def close_contradiction(self, cid: int, status: str = "explained") -> None:
        self.con.execute("UPDATE contradictions SET status=? WHERE id=?", (status, cid))
        self.con.commit()

    # ------------------------------------------------------------ reliability
    def reliability(self, source: str, domain: str | None = None) -> float:
        """Shrunk calibration score in [0,1]. Brier 0 -> 1.0, coin-flip (0.25) -> 0.5."""
        kind = source.split(":")[0]
        r0 = PRIOR_RELIABILITY.get(kind, 0.6)
        q = "SELECT brier FROM bets WHERE source=? AND brier IS NOT NULL"
        args: tuple = (source,)
        if domain:
            q += " AND domain=?"; args += (domain,)
        scores = [max(0.0, 1.0 - 2.0 * r["brier"]) for r in self.con.execute(q, args)]
        return (PRIOR_WEIGHT * r0 + sum(scores)) / (PRIOR_WEIGHT + len(scores))

    def calibration(self, source: str) -> dict:
        rows = self.con.execute(
            "SELECT prob, outcome, brier, domain FROM bets WHERE source=? AND brier IS NOT NULL",
            (source,)).fetchall()
        if not rows:
            return {"source": source, "n": 0, "reliability": round(self.reliability(source), 3)}
        mean_conf = sum(r["prob"] for r in rows) / len(rows)
        hit_rate = sum(r["outcome"] for r in rows) / len(rows)
        curve = []
        for lo in (0.0, 0.5, 0.6, 0.7, 0.8, 0.9):
            hi = {0.0: 0.5}.get(lo, lo + 0.1)
            b = [r for r in rows if lo <= r["prob"] < hi or (hi >= 1.0 and r["prob"] == 1.0)]
            if b:
                curve.append({"conf": f"{lo:.1f}-{min(hi, 1.0):.1f}", "n": len(b),
                              "said": round(sum(r["prob"] for r in b) / len(b), 3),
                              "was_right": round(sum(r["outcome"] for r in b) / len(b), 3)})
        domains = sorted({r["domain"] for r in rows if r["domain"]})
        return {"source": source, "n": len(rows),
                "mean_brier": round(sum(r["brier"] for r in rows) / len(rows), 4),
                "mean_conf": round(mean_conf, 3), "hit_rate": round(hit_rate, 3),
                "overconfidence": round(mean_conf - hit_rate, 3),
                "reliability": round(self.reliability(source), 3),
                "by_domain": {d: round(self.reliability(source, d), 3) for d in domains},
                "curve": curve}

    # ------------------------------------------------------------ names
    def propose_name(self, func: int, threshold: float = 0.9) -> tuple[str, float] | None:
        b = self.belief(func)
        lab, p = max(b.items(), key=lambda kv: kv[1])
        return (lab, p) if p >= threshold and lab != "unknown" else None

    def commit_name(self, func: int, name: str, prob: float) -> None:
        open_c = self.con.execute("SELECT 1 FROM contradictions WHERE func=? AND status='open'", (func,)).fetchone()
        if open_c:
            raise ValueError(f"FUNC {func} has an open contradiction; resolve it before naming")
        ev = [r["id"] for r in self.con.execute("SELECT id FROM evidence WHERE func=?", (func,))]
        self.con.execute("INSERT OR REPLACE INTO names (func, name, prob, provenance) VALUES (?,?,?,?)",
                         (func, name, prob, json.dumps({"evidence": ev})))
        self.con.commit()

    # ------------------------------------------------------------ experiment choice
    def expected_info_gain(self, func: int, outcome_model: dict[str, dict[str, float]]) -> float:
        """EIG of an experiment on ``func``.

        ``outcome_model[outcome][label]`` = P(outcome | label). Returns the
        expected drop in entropy (bits) of this function's belief.
        """
        prior = self.belief(func)
        h0 = entropy(prior)
        eig = 0.0
        for outcome, given in outcome_model.items():
            joint = {lab: prior[lab] * given.get(lab, 0.0) for lab in self.labels}
            po = sum(joint.values())
            if po <= 0:
                continue
            eig += po * (h0 - entropy({lab: v / po for lab, v in joint.items()}))
        return eig

    def most_uncertain(self, limit: int = 20) -> list[tuple[int, float]]:
        funcs = [r["num"] for r in self.con.execute("SELECT num FROM functions")]
        scored = [(f, entropy(self.belief(f))) for f in funcs]
        return sorted(scored, key=lambda t: -t[1])[:limit]


def _normalise(d: dict[str, float]) -> dict[str, float]:
    z = sum(d.values()) or 1.0
    return {k: v / z for k, v in d.items()}
