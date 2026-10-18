"""Durable experiment campaigns on Temporal (DECISIONS.md D11).

A campaign is a Temporal *workflow*: a loop whose progress Temporal saves step
by step, so it resumes after a crash or reboot. Each experiment (logging pass,
halt, knockout) is an *activity*: an ordinary function that wraps the existing
``loop.py`` / ``knockout.py`` call and is retried when the harness fails.

    temporal server start-dev --db-filename campaigns.db     # once, on the Mac
    python -m ffelab.workflows worker --db ffe.db --backend qemu
    python -m ffelab.workflows start plan.json

Needs the ``durable`` extra (``pip install -e '.[durable]'``). ``loop.py`` keeps
working without Temporal (lite mode).
"""
