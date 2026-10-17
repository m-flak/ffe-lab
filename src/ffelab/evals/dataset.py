"""Eval cases: what the agent sees, and the answer it is graded against.

Gold labels follow the ledger rules: a function is only a case when
experiment-only evidence is decisive (the same test ``settle_claims`` uses),
so no opinion, inherited name or static derivation ever becomes ground truth.
Red-team cases come from claims that experiments have already settled.

Each case stores a ``blind_view`` snapshot, so an eval runs without ffe.db or
the ffed3d asm (that is what lets CI run it), and a ``split``:

* ``dev``: tune prompts against these.
* ``test``: held out. Only look at the score, never at individual answers,
  or the prompt slowly overfits to them and the number stops meaning anything.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from .. import adversary as adv
from .. import labels as lbl
from ..ledger import Ledger
from ..models import FuncNum, Model, Probability, TopLabel

Task = Literal["blind_label", "red_team"]
Split = Literal["dev", "test"]
Provenance = Literal["experiment", "synthetic"]
TEST_FRACTION = 0.3


class TargetClaim(Model):
    """The claim a red-team case attacks (who made it is hidden from the red team)."""
    label: str
    confidence: Probability
    suggested_test: dict[str, Any] = Field(default_factory=dict)


class EvalCase(Model):
    id: str
    task: Task
    func: FuncNum
    view: dict[str, Any]                       # blind_view output, frozen at export time
    gold: TopLabel                             # experiment-settled top-level label
    gold_path: str | None = None
    claim: TargetClaim | None = None           # red-team cases only
    provenance: Provenance
    experiment_ids: list[int] = Field(default_factory=list)
    split: Split
    note: str | None = None

    @model_validator(mode="after")
    def _red_team_has_claim(self):
        if (self.task == "red_team") != (self.claim is not None):
            raise ValueError("red_team cases need a claim; blind_label cases must not have one")
        return self

    @property
    def claim_wrong(self) -> bool | None:
        return None if self.claim is None else lbl.top(self.claim.label) != self.gold


def split_for(key: str, test_fraction: float = TEST_FRACTION) -> Split:
    """Stable split from a hash, so re-exporting never moves a case between dev and test."""
    h = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return "test" if h < test_fraction else "dev"


def load(path: str | Path) -> list[EvalCase]:
    out = []
    for i, line in enumerate(Path(path).read_text().splitlines(), 1):
        if line.strip():
            try:
                out.append(EvalCase.model_validate_json(line))
            except ValueError as e:
                raise ValueError(f"{path}:{i}: {e}") from e
    return out


def save(cases: list[EvalCase], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("".join(c.model_dump_json(exclude_none=True) + "\n" for c in cases))


def digest(cases: list[EvalCase]) -> str:
    return hashlib.sha256("".join(c.model_dump_json() for c in cases).encode()).hexdigest()[:12]


# ---------------------------------------------------------------- export from the ledger
def _experiment_ids(ledger: Ledger, func: int) -> list[int]:
    return [r["e"] for r in ledger.con.execute(
        "SELECT DISTINCT experiment_id AS e FROM evidence WHERE func=? AND experiment_id IS NOT NULL "
        "ORDER BY e", (func,))]


def export_blind(ledger: Ledger, threshold: float = 0.95, min_evidence: int = 3) -> list[EvalCase]:
    """One blind-label case per function whose experiment-only belief is decisive."""
    cases = []
    for r in ledger.con.execute("SELECT num FROM functions ORDER BY num"):
        f = r["num"]
        dist, n = ledger.experimental_belief(f)
        if n < min_evidence:
            continue
        top, p = max(dist.items(), key=lambda kv: kv[1])
        if p < threshold:
            continue
        deep = lbl.deepest_label(ledger.con, f, top)
        path = deep["path"] if deep and deep["experiment_id"] is not None else None
        cases.append(EvalCase(id=f"blind-{f}", task="blind_label", func=f, view=adv.blind_view(ledger, f),
                              gold=top, gold_path=path, provenance="experiment",
                              experiment_ids=_experiment_ids(ledger, f), split=split_for(f"func-{f}")))
    return cases


def pre_experiment_test(claimed: str, labels: list[str]) -> dict:
    """The test ``red_team_targets`` would suggest before any experiment ran.

    The live tool picks the rival experiments favour once there are some; for a
    case whose answer *is* those experiments that would hand the red team the
    gold label, so the eval uses the no-evidence choice: the most testable rival."""
    others = [lab for lab in labels if lab not in (claimed, "unknown")]
    rival = max(others, key=lambda lab: abs(adv.design_test(claimed, lab, labels)["separation"]))
    return adv.design_test(claimed, rival, labels) | {"rival": rival}


def export_red_team(ledger: Ledger) -> list[EvalCase]:
    """One red-team case per claim that experiments have settled (right or wrong)."""
    cases = []
    for b in ledger.con.execute("SELECT * FROM bets WHERE kind='claim' AND outcome IS NOT NULL "
                                "AND source NOT LIKE 'adversary%' ORDER BY id"):
        dist, _ = ledger.experimental_belief(b["func"])
        gold = max(dist.items(), key=lambda kv: kv[1])[0]
        claimed = lbl.top(b["label"])
        if (claimed == gold) != bool(b["outcome"]):
            continue                    # evidence moved since settlement; leave it to a contradiction review
        cases.append(EvalCase(
            id=f"redteam-{b['id']}", task="red_team", func=b["func"], view=adv.blind_view(ledger, b["func"]),
            gold=gold, claim=TargetClaim(label=b["label"], confidence=b["prob"],
                                         suggested_test=pre_experiment_test(claimed, ledger.labels)),
            provenance="experiment", experiment_ids=[b["experiment_id"]],
            split=split_for(f"func-{b['func']}")))  # same split as the function's blind case: no leakage
    return cases
