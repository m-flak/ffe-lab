"""Activities: the experiment steps a campaign runs, each safe to retry.

Retry safety comes from a *run key* (workflow id + step id). The experiment row
and its run key are committed together, so an attempt that finds its run key
already recorded returns the stored result instead of running the experiment
again: evidence is never counted twice, which keeps the ledger rule that only
experiments settle bets honest under retries.

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

import json
import sqlite3
import threading
from typing import Callable

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .. import db as dbmod
from .. import knockout as ko
from ..harness.backend import GameBackend
from ..ledger import Ledger
from ..loop import halt_and_score, logging_pass, rank_targets
from .types import AutoPick, StepRequest, StepResult

STEPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS campaign_steps (
    run_key TEXT PRIMARY KEY,         -- workflow id / step id
    experiment_id INTEGER,
    result TEXT,                      -- JSON StepResult once the step finished
    created TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

HARNESS_ERRORS = (OSError, ConnectionError, TimeoutError, EOFError)


class HarnessError(Exception):
    """The emulator or debugger failed; retry the step on a fresh backend."""


def bad_input(msg: str) -> ApplicationError:
    """A step that can never succeed (unknown save state, bad kind): don't retry."""
    return ApplicationError(msg, type="BadStep", non_retryable=True)


class KeyedLedger(Ledger):
    """A Ledger whose experiments carry the run key of the step that made them."""

    def __init__(self, con, run_key: str, labels=None):
        super().__init__(con, labels)
        self.run_key = run_key
        self.experiment_id: int | None = None

    def record_experiment(self, kind, scenario, detail=None):
        if self.experiment_id is not None:
            raise RuntimeError(f"step {self.run_key} tried to record a second experiment")
        detail = dict(detail or {}, run_key=self.run_key)
        cur = self.con.execute("INSERT INTO experiments (kind, scenario, detail) VALUES (?,?,?)",
                               (kind, scenario, json.dumps(detail)))
        self.experiment_id = int(cur.lastrowid)
        try:
            self.con.execute("INSERT INTO campaign_steps (run_key, experiment_id) VALUES (?,?)",
                             (self.run_key, self.experiment_id))
        except sqlite3.IntegrityError:
            # A timed-out attempt that was still running got here first. Drop
            # this copy; the next attempt finds the run key and replays it.
            self.con.rollback()
            raise HarnessError(f"step {self.run_key} was recorded by an earlier attempt") from None
        self.con.commit()                        # experiment and run key land together
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
    """Holds the ledger path and the one backend for this worker's emulator."""

    def __init__(self, db_path: str, backend_factory: Callable[[], GameBackend],
                 outdir: str = "knockouts"):
        self.db_path = db_path
        self.backend_factory = backend_factory
        self.outdir = outdir
        self._backend: GameBackend | None = None
        self._lock = threading.Lock()
        con = dbmod.connect(db_path)
        con.executescript(STEPS_SCHEMA)
        con.close()

    def _connect(self):
        con = dbmod.connect(self.db_path)
        con.executescript(STEPS_SCHEMA)
        return con

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
        con = self._connect()
        try:
            row = con.execute("SELECT experiment_id, result FROM campaign_steps WHERE run_key=?",
                              (run_key,)).fetchone()
            if row is not None:
                return _replayed(req, row)
            fresh = bool(info and info.attempt > 1)      # last attempt may have left it broken
            be = Heartbeating(self.backend(fresh=fresh))
            led = KeyedLedger(con, run_key)
            try:
                res = _run(be, led, req, self.outdir)
            except HarnessError:
                self.reset_backend()
                raise
            except ApplicationError:
                raise
            except Exception as e:
                # A bug, not a flaky emulator: retrying would fail the same way.
                raise ApplicationError(f"{type(e).__name__}: {e}", type="StepBug",
                                       non_retryable=True) from e
            con.execute("UPDATE campaign_steps SET result=? WHERE run_key=?",
                        (res.model_dump_json(), run_key))
            con.commit()
            return res
        finally:
            con.close()

    @activity.defn
    def pick_targets(self, pick: AutoPick) -> list[StepRequest]:
        con = self._connect()
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


def _replayed(req: StepRequest, row) -> StepResult:
    if row["result"]:
        res = StepResult.model_validate_json(row["result"])
        res.replayed = True
        return res
    # The experiment committed but the attempt died before its evidence was all
    # written. Don't rerun (that would double-count); flag it for review instead.
    return StepResult(step_id=req.step_id, kind=req.kind, scenario=req.scenario,
                      experiment_id=row["experiment_id"], outcome="partial",
                      detail={"note": "attempt died after recording the experiment; evidence may be incomplete"},
                      replayed=True)
