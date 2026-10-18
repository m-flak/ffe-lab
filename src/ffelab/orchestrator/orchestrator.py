"""The orchestrator: the agent loop as a program.

Until now the loop ran inside a Claude Code chat: the model decided what to do
next, and nothing outside it enforced a budget or kept a record of the steps.
Here the loop is ordinary Python. Each round:

1. queue every open contradiction for human review (ledger rule: contradictions
   get reviewed by a person);
2. **blind labeller** workers, in parallel, label the most uncertain functions
   that have no blind label yet; each label is recorded as an ordinary claim
   and revealed against the other claims;
3. **red team** workers, in parallel, each look at one confident claim
   (``adversary.attack_targets``) and decide whether to attack it, with which
   rival and in which registered scenario;
4. for each attack, in order (one emulator): place the bets first, then run
   the logging pass, which resolves them and settles decisive claims;
5. queue the workers' ``ask_human`` questions.

Between rounds and before every worker the run checks its budget and stop
conditions (``budget.py``). Workers decide; only this code writes to the
ledger and runs experiments. Every step is a span (``tracing.py``).

The orchestrator is one process with one SQLite connection, so workers'
tools are read-only and all writes happen here, between awaits.
"""
from __future__ import annotations

import asyncio
from typing import Any

from pydantic import Field

from .. import adversary as adv
from .. import behavior
from ..harness.backend import GameBackend
from ..ledger import Ledger
from ..loop import logging_pass
from ..models import Model
from . import questions as qs
from . import tracing
from .budget import Budget, BudgetTracker, Spend, StopReason
from .questions import Question
from .workers import BlindTask, RedTeamPlan, RedTeamTask, Worker, WorkerResult


class AttackOutcome(Model):
    claim_bet: int
    func: int
    decision: str                       # attacked | declined | invalid | failed
    rival: str | None = None
    scenario: str | None = None
    p_claim_wrong: float | None = None
    experiment: int | None = None
    resolved: list[dict] = Field(default_factory=list)     # bets the run resolved
    settled: list[dict] = Field(default_factory=list)      # claims it made decisive
    note: str | None = None


class RoundReport(Model):
    round: int
    blind_labels: list[dict[str, Any]] = Field(default_factory=list)
    attacks: list[AttackOutcome] = Field(default_factory=list)
    bets_resolved: int = 0
    claims_settled: int = 0
    questions_added: int = 0
    worker_errors: list[str] = Field(default_factory=list)
    spend: Spend

    @property
    def progress(self) -> bool:
        return bool(self.blind_labels or self.bets_resolved or self.claims_settled)

    @property
    def idle(self) -> bool:
        return not self.blind_labels and not self.attacks


class RunReport(Model):
    worker: str
    budget: Budget
    rounds: list[RoundReport]
    stop: StopReason
    spend: Spend
    open_questions: list[Question]


class Orchestrator:
    def __init__(self, ledger: Ledger, backend: GameBackend, worker: Worker, budget: Budget | None = None,
                 blind_per_round: int = 4, attacks_per_round: int = 2, concurrency: int = 4,
                 blind_source: str = "ai:blind", adversary_source: str = "adversary",
                 log_source: str = "halt", progress=None):
        self.ledger, self.backend, self.worker = ledger, backend, worker
        self.budget = budget or Budget()
        self.tracker = BudgetTracker(self.budget)
        self.blind_per_round, self.attacks_per_round = blind_per_round, attacks_per_round
        self.concurrency = concurrency
        self.blind_source, self.adversary_source, self.log_source = blind_source, adversary_source, log_source
        self.progress = progress or (lambda msg: None)
        qs.ensure_schema(ledger.con)

    # ------------------------------------------------------------ run
    async def run(self) -> RunReport:
        rounds: list[RoundReport] = []
        stop: StopReason | None = None
        stale = 0
        with tracing.span("agents.run", {"openinference.span.kind": "CHAIN", "ffelab.worker": self.worker.name,
                                         "ffelab.budget": self.budget.model_dump()}) as s:
            for n in range(1, self.budget.max_rounds + 1):
                stop = self._should_stop()
                if stop:
                    break
                r = await self.run_round(n)
                rounds.append(r)
                if r.idle and not r.worker_errors:
                    stop = "nothing_to_do"
                    break
                stale = 0 if r.progress else stale + 1
                if stale >= self.budget.patience:
                    stop = "no_progress"
                    break
            stop = stop or self._should_stop() or "max_rounds"
            spend = self.tracker.snapshot()
            tracing.set_attrs(s, {"ffelab.stop": stop, "ffelab.rounds": len(rounds),
                                  **tracing.usage_attrs(spend.input_tokens, spend.output_tokens, spend.usd)})
        return RunReport(worker=self.worker.name, budget=self.budget, rounds=rounds, stop=stop, spend=spend,
                         open_questions=qs.open_questions(self.ledger.con))

    def _should_stop(self) -> StopReason | None:
        if (s := self.tracker.exhausted()):
            return s
        if len(qs.open_questions(self.ledger.con)) >= self.budget.max_open_questions:
            return "questions_waiting"
        return None

    async def run_round(self, n: int) -> RoundReport:
        self.sem = asyncio.Semaphore(self.concurrency)   # per round: bound to the running event loop
        with tracing.span(f"round {n}", {"openinference.span.kind": "CHAIN", "ffelab.round": n}) as s:
            added = len(qs.sync_contradictions(self.ledger))
            r = RoundReport(round=n, spend=self.tracker.snapshot())
            await self._blind_step(r, s)
            if not self.tracker.exhausted():
                await self._red_team_step(r, s)
            r.questions_added += added
            r.spend = self.tracker.snapshot()
            tracing.set_attrs(s, {"ffelab.blind_labels": len(r.blind_labels), "ffelab.attacks": len(r.attacks),
                                  "ffelab.bets_resolved": r.bets_resolved, "ffelab.claims_settled": r.claims_settled})
        self.progress(f"round {n}: {len(r.blind_labels)} blind labels, "
                      f"{sum(a.decision == 'attacked' for a in r.attacks)} attacks, {r.bets_resolved} bets resolved, "
                      f"{r.claims_settled} claims settled, ${r.spend.usd:.2f} spent")
        return r

    # ------------------------------------------------------------ workers
    async def _work(self, task, parent) -> WorkerResult | None:
        async with self.sem:
            if self.tracker.exhausted():              # checked again: the budget may have run out while queued
                return None
            res = await self.worker.run(task, self.budget.worker_max_turns,
                                        self.tracker.worker_cap(self.concurrency), parent)
        self.tracker.add(res.usage.cost_usd, res.usage.input_tokens, res.usage.output_tokens, res.error is not None)
        return res

    def _collect(self, r: RoundReport, res: WorkerResult) -> None:
        for q in res.questions:
            r.questions_added += qs.ask_draft(self.ledger.con, f"{res.worker}:{res.kind}", q) is not None
        if res.error:
            r.worker_errors.append(f"{res.kind} {res.task}: {res.error}")

    def blind_targets(self) -> list[int]:
        done = {row["func"] for row in self.ledger.con.execute(
            "SELECT DISTINCT func FROM blind_labels WHERE source=?", (self.blind_source,))}
        cands = [f for f, _ in self.ledger.most_uncertain(len(done) + 10 * self.blind_per_round) if f not in done]
        return cands[:self.blind_per_round]

    async def _blind_step(self, r: RoundReport, parent) -> None:
        tasks = [BlindTask(func=f) for f in self.blind_targets()]
        for t, res in zip(tasks, await asyncio.gather(*(self._work(t, parent) for t in tasks))):
            if res is None:
                continue
            self._collect(r, res)
            if res.output is None:
                continue
            v = res.output
            adv.record_blind(self.ledger, t.func, v.label, v.confidence, self.blind_source)
            reveal = adv.reveal(self.ledger, t.func)
            r.blind_labels.append({"func": t.func, "label": v.label, "confidence": v.confidence,
                                   "reasoning": v.reasoning, "reveal": reveal})

    def red_team_targets(self) -> list[dict]:
        return [t for t in adv.attack_targets(self.ledger, limit=self.attacks_per_round * 3)
                if not t["open_attacks"]][:self.attacks_per_round]

    async def _red_team_step(self, r: RoundReport, parent) -> None:
        scen = behavior.scenarios(self.ledger)
        tasks = [RedTeamTask(target=t, scenarios={k: sorted(v) for k, v in scen.items()})
                 for t in self.red_team_targets()]
        results = await asyncio.gather(*(self._work(t, parent) for t in tasks))
        for t, res in zip(tasks, results):           # experiments one at a time: one emulator
            if res is None:
                continue
            self._collect(r, res)
            out = AttackOutcome(claim_bet=t.target["claim_bet"], func=t.func, decision="failed", note=res.error)
            if isinstance(res.output, RedTeamPlan):
                out = self._attack(t, res.output, scen, parent)
                r.bets_resolved += len(out.resolved)
                r.claims_settled += len(out.settled)
            r.attacks.append(out)

    def _attack(self, t: RedTeamTask, plan: RedTeamPlan, scen: dict[str, set[str]], parent) -> AttackOutcome:
        out = AttackOutcome(claim_bet=t.target["claim_bet"], func=t.func, decision="declined",
                            rival=plan.rival, scenario=plan.scenario, p_claim_wrong=plan.p_claim_wrong,
                            note=plan.reasoning)
        if not plan.attack:
            return out
        if plan.scenario not in scen:
            out.decision, out.note = "invalid", f"scenario {plan.scenario!r} is not registered"
            return out
        try:                                         # the bets go in BEFORE the run
            adv.place_attack(self.ledger, t.target["claim_bet"], plan.scenario, plan.rival,
                             plan.rival_predicts, plan.p_claim_wrong, self.adversary_source)
        except ValueError as e:
            out.decision, out.note = "invalid", str(e)
            return out
        with tracing.span("experiment logging_pass", {"openinference.span.kind": "TOOL", "tool.name": "logging_pass",
                                                      "ffelab.scenario": plan.scenario, "ffelab.func": t.func},
                          parent) as s:
            run = logging_pass(self.backend, self.ledger, plan.scenario, [t.func], sorted(scen[plan.scenario]),
                               source=self.log_source)
            tracing.set_attrs(s, {"ffelab.experiment": run["experiment"], "output.value": run})
        out.decision, out.experiment = "attacked", run["experiment"]
        out.resolved, out.settled = run["resolved"], run["settled"]
        return out


def run(orchestrator: Orchestrator) -> RunReport:
    return asyncio.run(orchestrator.run())
