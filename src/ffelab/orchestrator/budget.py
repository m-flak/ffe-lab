"""Budgets and stop conditions for an orchestrator run.

An agent loop left alone keeps spending. A run has hard limits (dollars,
tokens, wall-clock time, rounds) and soft ones (stop when the human has
enough questions waiting, or when rounds stop producing anything). The
tracker is checked between rounds and before every worker starts, and each
worker gets a per-call cap so one runaway agent can't eat the whole budget.
"""
from __future__ import annotations

import time
from typing import Literal

from pydantic import Field

from ..models import Model

StopReason = Literal["budget_usd", "budget_tokens", "time", "max_rounds", "questions_waiting",
                     "nothing_to_do", "no_progress", "error"]


class Budget(Model):
    """Limits for one ``ffelab agents run``. None means no limit."""
    max_usd: float | None = Field(5.0, ge=0, description="Total spend across all workers.")
    max_tokens: int | None = Field(None, ge=0, description="Input + output tokens across all workers.")
    max_seconds: float | None = Field(3600, ge=0, description="Wall-clock time for the run.")
    max_rounds: int = Field(10, ge=1)
    worker_max_turns: int = Field(12, ge=1, description="Agent turns (model calls) per worker.")
    worker_max_usd: float | None = Field(0.5, ge=0, description="Spend cap per worker call.")
    max_open_questions: int = Field(10, ge=1, description="Stop and wait for the human at this many.")
    patience: int = Field(2, ge=1, description="Rounds in a row without progress before stopping.")


class Spend(Model):
    usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    workers: int = 0
    failed_workers: int = 0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class BudgetTracker:
    def __init__(self, budget: Budget, clock=time.monotonic):
        self.budget, self.clock = budget, clock
        self.t0 = clock()
        self.spend = Spend()

    def add(self, usd: float, input_tokens: int, output_tokens: int, failed: bool = False) -> None:
        s = self.spend
        s.usd += usd
        s.input_tokens += input_tokens
        s.output_tokens += output_tokens
        s.workers += 1
        s.failed_workers += failed

    def snapshot(self) -> Spend:
        self.spend.seconds = round(self.clock() - self.t0, 2)
        return self.spend.model_copy()

    def exhausted(self) -> StopReason | None:
        b, s = self.budget, self.snapshot()
        if b.max_usd is not None and s.usd >= b.max_usd:
            return "budget_usd"
        if b.max_tokens is not None and s.tokens >= b.max_tokens:
            return "budget_tokens"
        if b.max_seconds is not None and s.seconds >= b.max_seconds:
            return "time"
        return None

    def worker_cap(self, concurrent: int = 1) -> float | None:
        """Per-worker dollar cap: the configured cap, or this worker's share of what's left."""
        b = self.budget
        if b.max_usd is None:
            return b.worker_max_usd
        share = max(b.max_usd - self.spend.usd, 0.0) / max(concurrent, 1)
        return share if b.worker_max_usd is None else min(b.worker_max_usd, share)
