"""Activities: the experiment steps a campaign runs, each safe to retry.

Retry safety comes from a *run key* (workflow id + step id) on the experiment
row (``Ledger.record_experiment(..., run_key=, if_exists="raise")``). From the
experiment insert to the last evidence write, a step's ledger writes are one
transaction (``Ledger.atomic``), so a step is recorded whole or not at all:

* an attempt that finds its run key already recorded returns that experiment
  instead of running it again, so evidence is never counted twice (the ledger
  rule that only experiments settle bets stays honest under retries);
* a timed-out attempt whose thread was still stuck and wakes up later loses the
  key (``RunKeyExists``) and rolls back having written nothing;
* an attempt that dies mid-step leaves no row, so the retry runs it cleanly.

The transaction opens at the experiment insert, after the game interaction, so
an attempt stuck in the emulator holds no database lock.

Failures come in three kinds:
* the **game** crashing during a knockout is a result (``outcome="crash"``),
  recorded like any other;
* the **harness** failing (dead QEMU, closed GDB socket, monitor timeout) raises
  ``HarnessError``, which Temporal retries on a fresh backend. A hang shows up as
  missing heartbeats: every backend call sends one, so a call that never
  returns stops them and Temporal times the attempt out;
* anything else is a bug in ffelab, failed at once (``StepBug``, not retried).

Each attempt starts with ``load_state``, which also undoes any knockout patch a
dead attempt left behind (a save state restores memory).
"""
from __future__ import annotations

import threading
from contextlib import ExitStack
from typing import Callable

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .. import db as dbmod
from .. import knockout as ko
from ..harness.backend import GameBackend
from ..ledger import Ledger, RunKeyExists
from ..models import Experiment
from ..loop import halt_and_score, logging_pass, rank_targets
from .types import AutoPick, StepRequest, StepResult

HARNESS_ERRORS = (OSError, ConnectionError, TimeoutError, EOFError)


class HarnessError(Exception):
    """The emulator or debugger failed; retry the step on a fresh backend."""


def bad_input(msg: str) -> ApplicationError:
    """A step that can never succeed (unknown save state, bad kind): don't retry."""
    return ApplicationError(msg, type="BadStep", non_retryable=True)


class KeyedLedger(Ledger):
    """A Ledger for one campaign step: its experiment carries the step's run key,
    and everything from that insert to the end of the ``with`` block is one
    transaction."""

    def __init__(self, con, run_key: str, labels=None):
        super().__init__(con, labels)
        self.run_key = run_key
        self.experiment_id: int | None = None
        self._stack = ExitStack()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._stack.__exit__(*exc)         # commit, or roll back on an exception

    def record_experiment(self, kind, scenario, detail=None, run_key=None, if_exists="raise"):
        if self.experiment_id is not None:
            raise RuntimeError(f"step {self.run_key} tried to record a second experiment")
        self._stack.enter_context(self.atomic())
        self.experiment_id = super().record_experiment(kind, scenario, detail,
                                                       run_key=self.run_key, if_exists="raise")
        return self.experiment_id


class Heartbeating:
    """Wraps a backend: heartbeat before every call, translate harness failures."""

    def __init__(self, inner: GameBackend):
        self._inner = inner

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            if activity.in_activity():
                activity.heartbeat(name)
            try:
                return attr(*args, **kwargs)
            except KeyError as e:
                if name in ("load_state", "save_state"):
                    raise bad_input(f"unknown save state: {e}") from e
                raise
            except HARNESS_ERRORS as e:
                raise HarnessError(f"{name}: {e}") from e
        return call


class CampaignActivities:
    """Holds the ledger target and the one backend for this worker's emulator.

    ``db`` is a SQLite path or a ``postgresql://`` URL (see ``db.connect``)."""

    def __init__(self, db: str, backend_factory: Callable[[], GameBackend],
                 outdir: str = "knockouts"):
        self.db = db
        self.backend_factory = backend_factory
        self.outdir = outdir
        self._backend: GameBackend | None = None
        self._lock = threading.Lock()

    def backend(self, fresh: bool = False) -> GameBackend:
        with self._lock:
            if fresh or self._backend is None:
                self._backend = self.backend_factory()
            return self._backend

    def reset_backend(self) -> None:
        with self._lock:
            self._backend = None

    @activity.defn
    def run_step(self, req: StepRequest) -> StepResult:
        info = activity.info() if activity.in_activity() else None
        run_key = f"{info.workflow_id}/{req.step_id}" if info else req.step_id
        con = dbmod.connect(self.db)
        try:
            led = KeyedLedger(con, run_key)
            done = led.experiment_for_run(run_key)
            if done is not None:
                return _replayed(req, done)
            fresh = bool(info and info.attempt > 1)      # last attempt may have left it broken
            be = Heartbeating(self.backend(fresh=fresh))
            try:
                with led:
                    return _run(be, led, req, self.outdir)
            except HarnessError:
                self.reset_backend()
                raise
            except RunKeyExists:
                # An earlier attempt recorded this step while we were running.
                # Nothing of ours was written; the next attempt replays theirs.
                raise ApplicationError(f"step {run_key} was recorded by an earlier attempt",
                                       type="AlreadyRecorded") from None
            except ApplicationError:
                raise
            except Exception as e:
                # A bug, not a flaky emulator: retrying would fail the same way.
                raise ApplicationError(f"{type(e).__name__}: {e}", type="StepBug",
                                       non_retryable=True) from e
        finally:
            con.close()

    @activity.defn
    def pick_targets(self, pick: AutoPick) -> list[StepRequest]:
        con = dbmod.connect(self.db)
        try:
            led = Ledger(con)
            skip = set(pick.exclude)
            if pick.kind == "logging_pass":
                cands = [r["num"] for r in con.execute("SELECT num FROM functions") if r["num"] not in skip]
                funcs = [f for f, _ in rank_targets(led, cands, pick.context, k=pick.count)]
                return ([StepRequest(kind="logging_pass", scenario=pick.scenario, funcs=funcs,
                                     context=pick.context, keys=pick.keys)] if funcs else [])
            funcs = [f for f, _ in led.most_uncertain(pick.count + len(skip)) if f not in skip][:pick.count]
            return [StepRequest(kind=pick.kind, scenario=pick.scenario, funcs=[f],
                                context=pick.context, keys=pick.keys) for f in funcs]
        finally:
            con.close()


def _run(be, led: KeyedLedger, req: StepRequest, outdir: str) -> StepResult:
    keys = req.keys or None
    if req.kind == "logging_pass":
        r = logging_pass(be, led, req.scenario, req.funcs, req.context, keys=keys)
        hit = sum(1 for n in r["counts"].values() if n)
        return StepResult(step_id=req.step_id, kind=req.kind, scenario=req.scenario,
                          experiment_id=r["experiment"], outcome="hits",
                          detail={"hit": hit, "missed": len(req.funcs) - hit,
                                  "resolved": len(r["resolved"]), "settled": len(r["settled"])})
    if req.kind == "halt":
        snap = halt_and_score(be, led, req.scenario, req.funcs[0], req.context, keys=keys)
        return StepResult(step_id=req.step_id, kind=req.kind, scenario=req.scenario,
                          experiment_id=led.experiment_id, outcome="halted" if snap else "timeout",
                          detail={"func": req.funcs[0], "stack": snap.stack if snap else []})
    r = ko.knockout(be, led, req.scenario, req.funcs[0], keys=keys, outdir=outdir)
    return StepResult(step_id=req.step_id, kind=req.kind, scenario=req.scenario,
                      experiment_id=r["experiment"], outcome=r["outcome"],
                      detail={"func": req.funcs[0], "changed_frac": r.get("changed_frac"),
                              "diff_image": r.get("diff_image"), "next": r.get("next")})


def _replayed(req: StepRequest, exp: Experiment) -> StepResult:
    """Rebuild a step's result from the experiment an earlier attempt recorded."""
    d = exp.detail
    if exp.kind == "logging_pass":
        counts = d.get("counts", {})
        hit = sum(1 for n in counts.values() if n)
        outcome, detail = "hits", {"hit": hit, "missed": len(d.get("funcs", [])) - hit}
    elif exp.kind == "halt":
        outcome, detail = ("halted" if d.get("halted") else "timeout"), {"func": d.get("func")}
    else:
        outcome = d.get("outcome", "invisible")
        detail = {"func": d.get("func"), "changed_frac": d.get("changed_frac"),
                  "diff_image": (d.get("images") or {}).get("diff")}
    return StepResult(step_id=req.step_id, kind=req.kind, scenario=req.scenario,
                      experiment_id=exp.id, outcome=outcome, detail=detail, replayed=True)
