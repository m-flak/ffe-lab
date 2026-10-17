"""Command line for campaigns.

    python -m ffelab.workflows worker --db ffe.db --backend qemu --emu '{"code_base": 0}'   # your calibrated code_base (Stage 2)
    python -m ffelab.workflows start plan.json
    python -m ffelab.workflows status NAME
    python -m ffelab.workflows pause|resume|stop NAME

One worker per emulator: its task queue (default ``emulator-1``) is that
emulator's name, and the campaign's workflow id is ``campaign-<name>``, so
Temporal refuses a second campaign with the same name while one runs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from .. import db as dbmod
from .activities import CampaignActivities
from .campaign import Campaign
from .types import CampaignPlan

DEFAULT_QUEUE = "emulator-1"


def make_backend(kind: str, db_path: str, emu: dict):
    """Same choices as the MCP server: ``fake`` for dry runs, ``qemu``/``dosbox-x`` for the game."""
    if kind == "fake":
        from ..harness.fake import FakeBackend
        truth = emu.get("truth", {"demo": {}})
        # give every function a stripe on screen so dry-run knockouts have pixels to diff
        visuals = {sc: [(f, (0, 8 * i, 319, 8 * i + 6), (200, 200, 200)) for i, f in enumerate(sorted(rates))]
                   for sc, rates in truth.items()}
        return FakeBackend(truth, visuals=visuals, crashers=set(emu.get("crashers", [])))
    from ..exe import load_map
    from ..harness.emulator import EmulatorBackend, EmulatorConfig
    raw = dict(emu)
    binary = raw.pop("binary", None)
    con = dbmod.connect(db_path)
    pos = (load_map(con, binary, min_conf=0.7) if binary else
           {r["num"]: r["pos"] for r in con.execute("SELECT num,pos FROM functions WHERE pos IS NOT NULL")})
    return EmulatorBackend(EmulatorConfig(**raw | {"kind": kind}), pos)


def load_plan(path: str) -> CampaignPlan:
    with open(path) as fh:
        return CampaignPlan.model_validate_json(fh.read())


async def connect(address: str) -> Client:
    return await Client.connect(address, data_converter=pydantic_data_converter)


async def worker(a) -> None:
    emu = json.loads(a.emu)
    if a.backend == "fake" and "truth" in emu:            # JSON keys are strings
        emu["truth"] = {s: {int(f): r for f, r in t.items()} for s, t in emu["truth"].items()}
    acts = CampaignActivities(a.db, lambda: make_backend(a.backend, a.db, emu), outdir=a.outdir)
    client = await connect(a.address)
    # A few threads so a retry can start while a hung attempt's thread is still
    # stuck; the workflow itself never runs two steps at once.
    with ThreadPoolExecutor(max_workers=4) as pool:
        w = Worker(client, task_queue=a.queue, workflows=[Campaign],
                   activities=[acts.run_step, acts.pick_targets],
                   activity_executor=pool, max_concurrent_activities=4)
        print(f"worker on {a.queue} ({a.backend}), ledger {a.db}; Ctrl-C to stop")
        await w.run()


async def start(a) -> None:
    plan = load_plan(a.plan)
    client = await connect(a.address)
    h = await client.start_workflow(Campaign.run, plan, id=f"campaign-{plan.name}", task_queue=a.queue)
    print(f"started {h.id}; web UI: http://localhost:8233/namespaces/default/workflows/{h.id}")
    if a.wait:
        print((await h.result()).model_dump_json(indent=2))


async def control(a) -> None:
    client = await connect(a.address)
    h = client.get_workflow_handle(f"campaign-{a.name}")
    if a.cmd == "status":
        print(json.dumps(await h.query(Campaign.progress), indent=2))
    else:
        await h.signal(getattr(Campaign, a.cmd))
        print(f"sent {a.cmd} to campaign-{a.name}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m ffelab.workflows")
    ap.add_argument("--address", default="localhost:7233", help="Temporal server")
    ap.add_argument("--queue", default=DEFAULT_QUEUE, help="task queue = emulator name")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("worker")
    w.add_argument("--db", default="ffe.db")
    w.add_argument("--backend", default="fake", choices=["fake", "qemu", "dosbox-x"])
    w.add_argument("--emu", default="{}", help="JSON: EmulatorConfig fields (+ binary), or {truth} for fake")
    w.add_argument("--outdir", default="knockouts")
    s = sub.add_parser("start"); s.add_argument("plan"); s.add_argument("--wait", action="store_true")
    for c in ("status", "pause", "resume", "stop"):
        sub.add_parser(c).add_argument("name")
    a = ap.parse_args(argv)
    run = {"worker": worker, "start": start}.get(a.cmd, control)
    try:
        asyncio.run(run(a))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
