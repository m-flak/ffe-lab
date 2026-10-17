"""Durable campaigns (D11) against the fake game and a real local Temporal server.

The server is the Temporal CLI's dev server. Tests look for ``temporal`` on
PATH (or $TEMPORAL_CLI); without it the SDK tries to download one, and the
tests skip if that fails too.
"""
import asyncio
import os
import shutil
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

try:
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from ffelab.workflows.activities import CampaignActivities
    from ffelab.workflows.campaign import Campaign
    from ffelab.workflows.types import AutoPick, CampaignPlan, StepRequest
except ImportError:                      # the 'durable' extra isn't installed
    WorkflowEnvironment = None

from ffelab import db as dbmod
from ffelab.harness.fake import FakeBackend

DRAW, CORE, IDLE = 10, 40, 11
TRUTH = {"orbit": {DRAW: 1.0, CORE: 1.0}, "docked": {CORE: 1.0}}
VISUALS = {"orbit": [(DRAW, (40, 60, 200, 100), (235, 235, 235))]}


def step(kind, scenario, funcs, context=(), step_id=""):
    return StepRequest(kind=kind, scenario=scenario, funcs=funcs, context=list(context), step_id=step_id)


def make_db(path):
    con = dbmod.connect(path)
    con.executemany("INSERT INTO functions (num, name, prefix, asm_text) VALUES (?,NULL,NULL,'ret')",
                    [(DRAW,), (CORE,), (IDLE,)])
    con.commit()
    con.close()


class Shared:
    """State shared by every backend the factory builds, to script failures."""
    def __init__(self):
        self.built = 0
        self.drop_first_load = False      # first backend: load_state raises ConnectionError
        self.hang_first_wait = 0.0        # first backend: wait() blocks this many seconds
        self.buggy = False                # every backend: screenshot() raises ValueError
        self.screenshots = 0


def factory(shared: Shared):
    def build():
        shared.built += 1
        n = shared.built

        class Scripted(FakeBackend):
            def load_state(self, name):
                if n == 1 and shared.drop_first_load:
                    raise ConnectionError("GDB stub closed the connection")
                super().load_state(name)

            def wait(self, ms):
                if n == 1 and shared.hang_first_wait:
                    time.sleep(shared.hang_first_wait)
                super().wait(ms)

            def screenshot(self, path):
                shared.screenshots += 1
                if shared.buggy:
                    raise ValueError("bad image size")
                return super().screenshot(path)

        return Scripted(TRUTH, visuals=VISUALS, crashers={CORE})
    return build


def temporal_cli():
    return os.environ.get("TEMPORAL_CLI") or shutil.which("temporal")


@unittest.skipIf(WorkflowEnvironment is None, "temporalio not installed")
class TestCampaign(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loop = asyncio.new_event_loop()
        try:
            cls.env = cls.loop.run_until_complete(
                WorkflowEnvironment.start_local(dev_server_existing_path=temporal_cli(),
                                                data_converter=pydantic_data_converter))
        except Exception as e:                       # no CLI and no download
            cls.loop.close()
            raise unittest.SkipTest(f"no Temporal dev server: {e}")

    @classmethod
    def tearDownClass(cls):
        cls.loop.run_until_complete(cls.env.shutdown())
        cls.loop.close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "ffe.db")
        make_db(self.db)
        self.shared = Shared()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def campaign(self, plan, signals=()):
        acts = CampaignActivities(self.db, factory(self.shared), outdir=os.path.join(self.tmp, "ko"))
        queue = f"q-{uuid.uuid4()}"

        async def go():
            with ThreadPoolExecutor(max_workers=4) as pool:
                async with Worker(self.env.client, task_queue=queue, workflows=[Campaign],
                                  activities=[acts.run_step, acts.pick_targets],
                                  activity_executor=pool, max_concurrent_activities=4):
                    h = await self.env.client.start_workflow(
                        Campaign.run, plan, id=f"campaign-{uuid.uuid4()}", task_queue=queue)
                    for name, arg in signals:
                        await (h.signal(name, arg) if arg is not None else h.signal(name))
                    return await h.result()
        return self.loop.run_until_complete(go())

    def experiments(self):
        con = dbmod.connect(self.db)
        try:
            return [dict(r) for r in con.execute("SELECT kind, scenario FROM experiments")]
        finally:
            con.close()

    def test_runs_every_step_and_records_one_experiment_each(self):
        plan = CampaignPlan(name="basic", steps=[
            step("logging_pass", "orbit", [DRAW, CORE, IDLE], ["near_planet"]),
            step("knockout", "orbit", [DRAW]),
            step("knockout", "orbit", [CORE]),
            step("halt", "docked", [CORE], ["docked"]),
        ])
        r = self.campaign(plan)
        self.assertEqual((r.stopped, r.done, r.failed), ("finished", 4, 0))
        self.assertEqual(r.outcomes, {"hits": 1, "visible": 1, "crash": 1, "halted": 1})
        self.assertEqual(len(self.experiments()), 4)

    def test_harness_failure_is_retried_on_a_fresh_backend(self):
        self.shared.drop_first_load = True
        r = self.campaign(CampaignPlan(name="flaky", steps=[step("knockout", "orbit", [DRAW])]))
        self.assertEqual((r.done, r.failed, r.outcomes), (1, 0, {"visible": 1}))
        self.assertEqual(self.shared.built, 2)
        self.assertEqual(len(self.experiments()), 1)

    def test_hang_times_out_and_is_not_counted_twice(self):
        # The first attempt blocks in wait() past the heartbeat timeout. Temporal
        # retries on a fresh backend; when the stuck thread wakes up it finds the
        # step already recorded and drops its copy.
        self.shared.hang_first_wait = 5.0
        plan = CampaignPlan(name="hang", steps=[step("knockout", "orbit", [DRAW])], heartbeat_s=2)
        r = self.campaign(plan)
        self.assertEqual((r.done, r.failed), (1, 0))
        self.assertEqual(self.shared.built, 2)            # the retry got a fresh backend
        time.sleep(self.shared.hang_first_wait)          # let the stuck thread finish
        self.assertEqual(len(self.experiments()), 1)

    def test_unknown_save_state_fails_once_and_campaign_continues(self):
        plan = CampaignPlan(name="bad", steps=[step("knockout", "no_such_state", [DRAW]),
                                          step("knockout", "orbit", [DRAW])])
        r = self.campaign(plan)
        self.assertEqual((r.done, r.failed), (1, 1))
        self.assertIn("unknown save state", r.errors[0])
        self.assertEqual(self.shared.built, 1)            # not retried

    def test_a_bug_fails_once_without_retries(self):
        self.shared.buggy = True
        r = self.campaign(CampaignPlan(name="bug", steps=[step("knockout", "orbit", [DRAW])]))
        self.assertEqual((r.done, r.failed), (0, 1))
        self.assertIn("ValueError", r.errors[0])
        self.assertEqual(self.shared.screenshots, 1)      # one attempt only

    def test_auto_pick_and_add_steps_signal(self):
        plan = CampaignPlan(name="auto", auto=[AutoPick(kind="knockout", scenario="orbit", count=2)])
        r = self.campaign(plan, signals=[("add_steps", [step("halt", "orbit", [DRAW])])])
        self.assertEqual((r.stopped, r.done, r.failed), ("finished", 3, 0))

    def test_bad_plan_is_rejected_before_anything_runs(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            CampaignPlan(name="x", steps=[step("knockout", "orbit", [])])
        with self.assertRaises(ValidationError):
            StepRequest(kind="dance", scenario="orbit", funcs=[DRAW])

    def test_budget_stops_the_campaign(self):
        plan = CampaignPlan(name="budget", steps=[step("knockout", "orbit", [DRAW])] * 3, max_steps=2)
        r = self.campaign(plan)
        self.assertEqual((r.stopped, r.done), ("budget", 2))


@unittest.skipIf(WorkflowEnvironment is None, "temporalio not installed")
class TestRunKeys(unittest.TestCase):
    """Retry safety without a server: call the activity directly twice."""

    def test_same_step_twice_records_once(self):
        tmp = tempfile.mkdtemp()
        try:
            db = os.path.join(tmp, "ffe.db")
            make_db(db)
            acts = CampaignActivities(db, factory(Shared()), outdir=os.path.join(tmp, "ko"))
            req = step("knockout", "orbit", [DRAW], step_id="s00001")
            first = acts.run_step(req)
            again = acts.run_step(req)
            self.assertFalse(first.replayed)
            self.assertTrue(again.replayed)
            self.assertEqual(first.experiment_id, again.experiment_id)
            con = dbmod.connect(db)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM experiments").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 1)
            con.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
