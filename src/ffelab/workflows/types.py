"""Inputs and outputs passed between the campaign workflow and its activities.

Pydantic models on the shared ``models.Model`` base, so a malformed plan or
step is rejected at the boundary. They travel through Temporal with
``temporalio.contrib.pydantic.pydantic_data_converter``; every client and
worker must use it. Keep them small (Temporal caps payloads at ~2 MB), so
screenshots travel as file paths, never bytes.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from ..models import FuncNum, Model

StepKind = Literal["logging_pass", "halt", "knockout"]
Outcome = Literal["hits", "halted", "timeout", "visible", "invisible", "crash", "partial"]


class StepRequest(Model):
    """One experiment. ``funcs`` is the breakpoint list for a logging pass;
    halt and knockout use ``funcs[0]``."""
    kind: StepKind
    scenario: str = Field(min_length=1)
    funcs: list[FuncNum] = Field(min_length=1)
    context: list[str] = Field(default_factory=list)
    keys: list[str] = Field(default_factory=list)
    step_id: str = ""           # set by the workflow; part of the run key


class StepResult(Model):
    step_id: str
    kind: StepKind
    scenario: str
    experiment_id: int | None
    outcome: Outcome
    detail: dict[str, Any] = Field(default_factory=dict)
    replayed: bool = False      # True when an earlier attempt had already recorded it


class AutoPick(Model):
    """Let the campaign choose its own targets: the ``count`` functions with the
    highest expected information gain (logging pass) or entropy (halt/knockout)."""
    kind: StepKind
    scenario: str = Field(min_length=1)
    count: int = Field(ge=1)
    context: list[str] = Field(default_factory=list)
    keys: list[str] = Field(default_factory=list)
    exclude: list[FuncNum] = Field(default_factory=list)


class CampaignState(Model):
    """Carried across continue-as-new so long campaigns keep a short history."""
    done: int = 0
    failed: int = 0
    consecutive_failures: int = 0
    next_id: int = 0
    auto_done: int = 0
    paused: bool = False
    pending: list[StepRequest] = Field(default_factory=list)
    outcomes: dict[str, int] = Field(default_factory=dict)
    experiments: list[int] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class CampaignPlan(Model):
    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")   # workflow id is campaign-<name>
    steps: list[StepRequest] = Field(default_factory=list)
    auto: list[AutoPick] = Field(default_factory=list)
    max_steps: int = Field(500, ge=1)            # budget; the campaign stops here
    stop_after_failures: int = Field(5, ge=1)    # consecutive failed steps before giving up
    heartbeat_s: int = Field(30, ge=1)           # a backend call blocked this long counts as a hang
    step_timeout_s: int = Field(600, ge=1)       # hard limit for one attempt of one step
    resume: CampaignState | None = None          # set by the workflow when it continues as new


class CampaignReport(Model):
    name: str
    done: int
    failed: int
    stopped: Literal["finished", "budget", "stopped", "failures"]
    outcomes: dict[str, int] = Field(default_factory=dict)
    experiments: list[int] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
