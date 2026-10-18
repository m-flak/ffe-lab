"""Scores for one eval run.

Terms, briefly:

* **Accuracy**: share of cases where the label matches the experiment-settled one.
  Reported per subsystem too, because an agent can be great at ``render`` and
  useless at ``economy``, and the average hides it.
* **Brier score**: mean of (stated probability - outcome)^2. 0 is perfect,
  0.25 is what always saying 50% gets you. Punishes confident mistakes hardest,
  which is the failure the ledger cares about most (DECISIONS D3). For the
  blind labeller the probability is its confidence that its label is right;
  for the red team it is ``p_claim_wrong`` against whether the claim was wrong.
  A failed call (refusal, invalid output) counts as a wrong answer with a 50%
  forecast. Note an agent that always says "unknown, 20%" gets a decent Brier
  and a terrible accuracy: read the two together.
* **ECE (expected calibration error)**: bucket answers by stated confidence and
  compare each bucket's average confidence with how often it was right. 0 means
  "when it says 80% it is right 80% of the time".
* **Bootstrap interval**: resample the cases many times and recompute accuracy;
  the middle 95% of those values shows how much of a difference between two
  runs could be luck. Small datasets give wide intervals.
* **Cost per correct label**: dollars spent divided by labels it got right.
"""
from __future__ import annotations

import random
from collections import defaultdict

from ..models import BlindLabelVerdict, Model, RedTeamVerdict
from .dataset import EvalCase
from .solvers import SolverOutput


class CaseResult(Model):
    case_id: str
    split: str
    gold: str
    output: SolverOutput
    correct: bool                 # blind: label == gold; red team: attack decision right
    forecast: float               # the probability the agent stated for ``event``
    event: bool                   # blind: "my label is right"; red team: "the claim is wrong"


class Metrics(Model):
    n: int
    errors: int
    accuracy: float
    accuracy_ci95: tuple[float, float]
    brier: float
    ece: float
    mean_confidence: float                      # mean stated probability (see CaseResult.forecast)
    overconfidence: float                       # mean stated probability - how often it happened
    by_subsystem: dict[str, dict[str, float]]   # gold label -> {n, accuracy, brier}
    abstain_rate: float | None = None           # blind: share answered "unknown"
    refute_precision: float | None = None       # red team: attacked claims that were wrong
    refute_recall: float | None = None          # red team: wrong claims that were attacked
    rival_accuracy: float | None = None         # red team: right rival, among wrong claims attacked
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cost_per_correct_usd: float | None


def grade(case: EvalCase, out: SolverOutput) -> CaseResult:
    v = out.verdict
    if case.task == "red_team":
        event = bool(case.claim_wrong)
        if isinstance(v, RedTeamVerdict):
            correct, forecast = v.attack == event, v.p_claim_wrong
        else:                                   # error: wrong decision, a coin-flip forecast
            correct, forecast = False, 0.5
    elif isinstance(v, BlindLabelVerdict):
        correct = event = v.label == case.gold
        forecast = v.confidence
    else:
        correct = event = False
        forecast = 0.5
    return CaseResult(case_id=case.id, split=case.split, gold=case.gold, output=out,
                      correct=correct, forecast=forecast, event=event)


def _mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def _brier(rs: list[CaseResult]) -> float:
    return _mean((r.forecast - float(r.event)) ** 2 for r in rs)


def _ece(rs: list[CaseResult], bins: int = 5) -> float:
    total = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        in_bin = [r for r in rs if lo <= r.forecast < hi or (b == bins - 1 and r.forecast == 1.0)]
        if in_bin:
            total += len(in_bin) / len(rs) * abs(_mean(r.forecast for r in in_bin)
                                                 - _mean(float(r.event) for r in in_bin))
    return total


def bootstrap_ci(values: list[bool], iters: int = 2000, seed: int = 0) -> tuple[float, float]:
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    stats = sorted(sum(rng.choice(values) for _ in range(n)) / n for _ in range(iters))
    return (round(stats[int(0.025 * iters)], 4), round(stats[int(0.975 * iters) - 1], 4))


def compute(cases: list[EvalCase], results: list[CaseResult]) -> Metrics:
    by_id = {c.id: c for c in cases}
    n = len(results)
    acc = _mean(float(r.correct) for r in results)
    groups: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        groups[r.gold].append(r)
    by_sub = {g: {"n": len(rs), "accuracy": round(_mean(float(r.correct) for r in rs), 4),
                  "brier": round(_brier(rs), 4)} for g, rs in sorted(groups.items())}
    cost = sum(r.output.usage.cost_usd for r in results)
    n_correct = sum(r.correct for r in results)
    extra: dict = {}
    verdicts = [(by_id[r.case_id], r.output.verdict) for r in results]
    if any(c.task == "blind_label" for c, _ in verdicts):
        extra["abstain_rate"] = round(_mean(isinstance(v, BlindLabelVerdict) and v.label == "unknown"
                                            for _, v in verdicts), 4)
    if any(c.task == "red_team" for c, _ in verdicts):
        attacked = [(c, v) for c, v in verdicts if isinstance(v, RedTeamVerdict) and v.attack]
        wrong = [c for c, _ in verdicts if c.claim_wrong]
        hits = [(c, v) for c, v in attacked if c.claim_wrong]
        extra["refute_precision"] = round(len(hits) / len(attacked), 4) if attacked else None
        extra["refute_recall"] = round(len(hits) / len(wrong), 4) if wrong else None
        extra["rival_accuracy"] = round(_mean(v.rival == c.gold for c, v in hits), 4) if hits else None
    mean_conf = _mean(r.forecast for r in results)
    event_rate = _mean(float(r.event) for r in results)
    return Metrics(
        n=n, errors=sum(r.output.error is not None for r in results),
        accuracy=round(acc, 4), accuracy_ci95=bootstrap_ci([r.correct for r in results]),
        brier=round(_brier(results), 4), ece=round(_ece(results), 4) if results else 0.0,
        mean_confidence=round(mean_conf, 4), overconfidence=round(mean_conf - event_rate, 4),
        by_subsystem=by_sub,
        input_tokens=sum(r.output.usage.input_tokens for r in results),
        output_tokens=sum(r.output.usage.output_tokens for r in results),
        cost_usd=round(cost, 6),
        cost_per_correct_usd=round(cost / n_correct, 6) if n_correct and cost else None,
        **extra)
