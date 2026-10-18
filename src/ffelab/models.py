"""Pydantic models: the shared schema for every core record.

One model per thing the ledger stores (experiments, evidence, bets,
contradictions, blind labels, knockouts, names) plus the halt snapshot, the
inputs agents send to tools, and the structured answers LLM agents return.

Why bother: an LLM's output is just text until something checks it. With
these models a confidence of 1.3, a label of "graphics" or a bet with no
machine-checkable criterion is rejected at the boundary with a readable
error, instead of being parsed out of free text and quietly stored. The same
models describe the SQLite rows (``from_row``), so the Postgres move (PLAN
step 3) and the orchestrator (step 2) share one definition of every record;
``tests/test_models.py`` fails if a table and its model drift apart.

``pydantic.ValidationError`` subclasses ``ValueError``, so callers that
already catch ``ValueError`` keep working.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Annotated, Any, ClassVar, Literal, get_args

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from . import labels as lbl

# The 13 fixed top-level subsystems beliefs are kept over (DECISIONS D5).
TopLabel = Literal["render", "physics", "ai", "ui", "mission", "economy", "sysgen",
                   "io", "audio", "math", "text", "platform", "unknown"]
TOP_LABELS: tuple[str, ...] = get_args(TopLabel)


def _label_path(v: str) -> str:
    return lbl.validate(v, list(TOP_LABELS))


Probability = Annotated[float, Field(ge=0.0, le=1.0)]
LabelPath = Annotated[str, AfterValidator(_label_path)]   # e.g. render/planet/atmosphere
FuncNum = Annotated[int, Field(ge=0)]
Expect = Literal["hit", "miss"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- halts
class HaltSnapshot(Model):
    """Everything captured when a breakpoint freezes the game at one function."""
    func: FuncNum                                   # function number halted at
    scenario: str                                   # save state / scenario name
    hits: int = Field(1, ge=0)                      # hits observed in the logging pass
    frames: int = Field(1, ge=0)                    # frames elapsed in that pass
    stack: list[int] = Field(default_factory=list)  # callers, innermost first
    regs: dict[str, int] = Field(default_factory=dict)
    args: list[int] = Field(default_factory=list)   # first stack args
    pointer_regions: list[str] = Field(default_factory=list)  # named regions args point into
    value_kinds: list[str] = Field(default_factory=list)      # e.g. "ascii", "fixed16", "int64"
    mem_reads: list[int] = Field(default_factory=list)
    mem_writes: list[int] = Field(default_factory=list)
    context: list[str] = Field(default_factory=list)          # scenario tags: docked, combat, ...
    trigger: str | None = None                      # input that preceded the hit, e.g. "F1"
    screenshot: str | None = None

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    @property
    def hits_per_frame(self) -> float:
        return self.hits / max(self.frames, 1)

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, s: str) -> "HaltSnapshot":
        return cls.model_validate_json(s)


# ---------------------------------------------------------------- ledger records
class Record(Model):
    """A row of one SQLite table. ``table`` names it; ``_json`` lists TEXT columns holding JSON."""
    table: ClassVar[str]
    _json: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def from_row(cls, row: sqlite3.Row | dict):
        d = dict(row)
        for k in cls._json:
            if isinstance(d.get(k), str):
                d[k] = json.loads(d[k])
        return cls.model_validate(d)


ExperimentKind = Literal["logging_pass", "halt", "knockout", "unicorn", "static_check"]


class Experiment(Record):
    """A recorded run. Only experiments resolve bets (DECISIONS D3).

    Reserved for PLAN step 4 (durable campaigns), not implemented yet: a unique
    ``run_key`` so a retried step returns the existing experiment instead of
    recording it twice, and keeping a game crash (a result, e.g. knockout
    health ``crashed``) separate from a harness failure (an error to retry).
    """
    table = "experiments"
    _json = ("detail",)
    id: int
    kind: ExperimentKind
    scenario: str
    detail: dict[str, Any] = Field(default_factory=dict)
    created: datetime | None = None


EvidenceKind = Literal["call", "static_callee", "log_hit", "log_miss", "halt", "halt_timeout",
                       "knockout", "knockout_obs"]


class Evidence(Record):
    """One observation: a likelihood over top-level labels for one function."""
    table = "evidence"
    _json = ("likelihoods",)
    id: int
    func: FuncNum
    source: str
    kind: EvidenceKind
    likelihoods: dict[str, Annotated[float, Field(ge=0.0)]]
    detail: str | None = None
    experiment_id: int | None = None
    created: datetime | None = None

    @property
    def is_opinion(self) -> bool:
        return self.kind == "call"

    @property
    def is_experimental(self) -> bool:
        return self.kind not in ("call", "static_callee")


class BetSpec(Model):
    """Machine-checkable prediction.

    Logging-pass specs carry exactly one of ``expect`` (hit | miss), ``min_rate``
    or ``max_rate``. Knockout specs set ``knockout`` and expect visible |
    invisible | crash."""
    scenario: str
    func: FuncNum
    knockout: bool = False
    expect: Literal["hit", "miss", "visible", "invisible", "crash"] | None = None
    min_rate: Probability | None = None
    max_rate: Probability | None = None

    @model_validator(mode="after")
    def _one_criterion(self):
        if self.knockout:
            if self.expect not in ("visible", "invisible", "crash") or self.min_rate or self.max_rate:
                raise ValueError("a knockout spec expects visible, invisible or crash")
            return self
        given = [x for x in (self.expect, self.min_rate, self.max_rate) if x is not None]
        if len(given) != 1 or self.expect in ("visible", "invisible", "crash"):
            raise ValueError("a bet spec needs exactly one of expect (hit/miss), min_rate, max_rate")
        return self


BetKind = Literal["claim", "rival", "prediction"]


class Bet(Record):
    """A claim, a rival claim or a prediction, with an honest probability. Brier-scored on resolution."""
    table = "bets"
    _json = ("spec",)
    id: int
    source: str
    domain: str | None = None
    func: FuncNum | None = None
    claim: str
    prediction: str
    prob: Probability
    outcome: bool | None = None
    brier: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    created: datetime | None = None
    resolved: datetime | None = None
    kind: BetKind = "prediction"
    label: LabelPath | None = None
    spec: BetSpec | None = None
    experiment_id: int | None = None       # the experiment that resolved it
    parent_id: int | None = None           # rival bets point at the claim they attack

    @model_validator(mode="after")
    def _consistent(self):
        if self.kind in ("claim", "rival") and (self.label is None or self.func is None):
            raise ValueError(f"a {self.kind} bet needs a func and a label")
        if self.outcome is not None and self.experiment_id is None:
            raise ValueError("a resolved bet must cite the experiment that resolved it")
        return self


ContradictionStatus = Literal["open", "explained", "claim_withdrawn"]


class Contradiction(Record):
    table = "contradictions"
    id: int
    func: FuncNum
    bet_id: int
    evidence_id: int | None = None
    claimed: TopLabel
    favoured: TopLabel
    ratio: float
    detail: str | None = None
    status: ContradictionStatus = "open"
    created: datetime | None = None


class BlindLabel(Record):
    table = "blind_labels"
    id: int
    func: FuncNum
    source: str
    label: LabelPath
    confidence: Probability
    revealed: bool = False
    agrees: bool | None = None
    created: datetime | None = None


class FuncLabel(Record):
    """A label path recorded on a function (claim, or observation tied to an experiment)."""
    table = "func_labels"
    id: int
    func: FuncNum
    path: LabelPath
    source: str
    confidence: Probability | None = None
    experiment_id: int | None = None
    note: str | None = None
    created: datetime | None = None


KnockoutHealth = Literal["ok", "crashed", "frozen"]


class Knockout(Record):
    table = "knockouts"
    _json = ("bbox",)
    id: int
    experiment_id: int
    func: FuncNum
    scenario: str
    health: KnockoutHealth
    changed_frac: float | None = None
    bbox: list[int] | None = None
    baseline: str | None = None
    knocked: str | None = None
    diff_image: str | None = None
    created: datetime | None = None


class Name(Record):
    table = "names"
    _json = ("provenance",)
    func: FuncNum
    name: str
    prob: Probability
    provenance: dict[str, Any] = Field(default_factory=dict)
    written: datetime | None = None


# ---------------------------------------------------------------- tool inputs
class ClaimInput(Model):
    """``record_call``: a source asserts a label with a confidence (an opinion, never a settlement)."""
    func: FuncNum
    label: LabelPath
    confidence: Probability
    source: str = Field("human", min_length=1)
    domain: str | None = None


class BlindLabelInput(Model):
    """``record_blind_label``: a label made from ``blind_view`` only."""
    func: FuncNum
    label: LabelPath
    confidence: Probability
    source: str = Field(min_length=1)


class AttackInput(Model):
    """``place_attack``: bet against a claim with a rival label and what the rival predicts."""
    claim_bet: int
    scenario: str = Field(min_length=1)
    rival: LabelPath
    expect: Expect
    prob: Probability
    source: str = "adversary"


class AttackResult(Model):
    rival_bet: int
    prediction_bet: int
    run: dict[str, Any]


class KnockoutObservationInput(Model):
    """``record_knockout_observation``: what vanished, as a label path tied to a knockout."""
    experiment_id: int
    func: FuncNum
    path: LabelPath
    confidence: Probability
    description: str = Field(min_length=1)
    source: str = Field(min_length=1)


class ResolvedBet(Model):
    bet: int
    source: str
    outcome: bool
    brier: float


class SettledClaim(ResolvedBet):
    label: str
    settled_as: TopLabel


class LoggingPassResult(Model):
    experiment: int
    counts: dict[int, int]
    resolved: list[ResolvedBet]
    settled: list[SettledClaim]


# ---------------------------------------------------------------- agent outputs (structured LLM answers)
class BlindLabelVerdict(Model):
    """What the blind labeller returns. "unknown" with low confidence is a valid answer."""
    label: TopLabel = Field(description="One top-level subsystem label.")
    confidence: Probability = Field(description="Honest probability that the label is right.")
    reasoning: str = Field(description="One sentence of reasoning from the code.")


class RedTeamVerdict(Model):
    """What the red team returns for one target, before any experiment runs."""
    p_claim_wrong: Probability = Field(description="Honest probability that the claimed label is wrong.")
    attack: bool = Field(description="Whether to place an attack bet on this claim.")
    rival: TopLabel | None = Field(None, description="The rival label if attacking.")
    rival_predicts: Expect | None = Field(
        None, description="What the rival predicts in the suggested scenario: hit or miss.")
    reasoning: str = Field(description="One or two sentences from the code.")

    @model_validator(mode="after")
    def _attack_needs_rival(self):
        if self.attack and (self.rival is None or self.rival_predicts is None):
            raise ValueError("an attack needs a rival and what the rival predicts")
        return self


RECORDS: tuple[type[Record], ...] = (Experiment, Evidence, Bet, Contradiction, BlindLabel,
                                     FuncLabel, Knockout, Name)
