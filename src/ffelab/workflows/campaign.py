"""The campaign workflow: run experiment steps one at a time until done.

Workflow code is replayed from Temporal's saved history to rebuild its state,
so it must be *deterministic*: no I/O, clock, random or ledger access here.
Everything that touches the world happens in the activities.

Steer a running campaign with signals (messages to a running workflow):
``pause``, ``resume``, ``stop``, ``add_steps``; read it with the ``progress``
query.
"""
from datetime import timedelta
from typing import Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from .activities import CampaignActivities
    from .types import CampaignPlan, CampaignReport, CampaignState, StepRequest

STEP_RETRY = RetryPolicy(initial_interval=timedelta(seconds=5), backoff_coefficient=2.0,
                         maximum_interval=timedelta(minutes=1), maximum_attempts=3,
                         non_retryable_error_types=["BadStep", "StepBug"])
STEPS_PER_RUN = 200                          # then continue-as-new to keep history short


@workflow.defn
class Campaign:
    def __init__(self) -> None:
        self.plan: Optional[CampaignPlan] = None
        self.state = CampaignState()
        self.paused = False
        self.stop_requested = False

    @workflow.run
    async def run(self, plan: CampaignPlan) -> CampaignReport:
        # one argument only: Temporal skips type conversion when the argument
        # count differs from the signature, so resume state rides in the plan
        self.plan = plan
        state, plan.resume = plan.resume, None
        if state is None:
            state = CampaignState()
            self._enqueue(state, plan.steps)
        early, self.state = self.state.pending, state     # add_steps signalled before run()
        self._enqueue(state, early)
        self.paused = self.paused or state.paused
        steps_this_run = 0

        while True:
            await workflow.wait_condition(lambda: not self.paused or self.stop_requested)
            if self.stop_requested:
                return self._report("stopped")
            if state.done + state.failed >= plan.max_steps:
                return self._report("budget")
            if state.consecutive_failures >= plan.stop_after_failures:
                return self._report("failures")
            if not state.pending:
                if state.auto_done >= len(plan.auto):
                    return self._report("finished")
                pick = plan.auto[state.auto_done]
                state.auto_done += 1
                try:
                    picked = await workflow.execute_activity_method(
                        CampaignActivities.pick_targets, pick,
                        start_to_close_timeout=timedelta(minutes=5), retry_policy=STEP_RETRY)
                except ActivityError as e:
                    self._failed(state, f"auto {pick.kind} {pick.scenario}", e)
                    continue
                self._enqueue(state, picked)
                continue

            req = state.pending.pop(0)
            try:
                res = await workflow.execute_activity_method(
                    CampaignActivities.run_step, req,
                    start_to_close_timeout=timedelta(seconds=plan.step_timeout_s),
                    heartbeat_timeout=timedelta(seconds=plan.heartbeat_s),
                    retry_policy=STEP_RETRY)
                state.done += 1
                state.consecutive_failures = 0
                state.outcomes[res.outcome] = state.outcomes.get(res.outcome, 0) + 1
                if res.experiment_id is not None:
                    state.experiments.append(res.experiment_id)
            except ActivityError as e:
                self._failed(state, f"{req.step_id} {req.kind} {req.scenario} {req.funcs[:3]}", e)

            steps_this_run += 1
            if steps_this_run >= STEPS_PER_RUN or workflow.info().is_continue_as_new_suggested():
                await workflow.wait_condition(workflow.all_handlers_finished)
                state.paused = self.paused
                plan.resume = state
                workflow.continue_as_new(plan)

    def _failed(self, state: CampaignState, what: str, e: ActivityError) -> None:
        state.failed += 1
        state.consecutive_failures += 1
        cause = e.cause if e.cause is not None else e
        state.errors = state.errors[-49:] + [f"{what}: {cause}"]

    def _enqueue(self, state: CampaignState, steps: list[StepRequest]) -> None:
        for s in steps:
            state.next_id += 1
            s.step_id = f"s{state.next_id:05d}"
            state.pending.append(s)

    def _report(self, why: str) -> CampaignReport:
        s = self.state
        return CampaignReport(name=self.plan.name, done=s.done, failed=s.failed, stopped=why,
                              outcomes=dict(s.outcomes), experiments=list(s.experiments),
                              errors=list(s.errors))

    # ------------------------------------------------------------ steering
    @workflow.signal
    def pause(self) -> None:
        self.paused = True

    @workflow.signal
    def resume(self) -> None:
        self.paused = False

    @workflow.signal
    def stop(self) -> None:
        self.stop_requested = True

    @workflow.signal
    def add_steps(self, steps: list[StepRequest]) -> None:
        self._enqueue(self.state, steps)

    @workflow.query
    def progress(self) -> dict:
        s = self.state
        return {"done": s.done, "failed": s.failed, "pending": len(s.pending),
                "paused": self.paused, "outcomes": dict(s.outcomes),
                "last_errors": s.errors[-5:]}
