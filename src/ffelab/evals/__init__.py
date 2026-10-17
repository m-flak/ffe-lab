"""Evals: score the blind labeller and red team against experiment-confirmed labels.

A small custom harness rather than Inspect AI or promptfoo: cases, solvers,
scorers and a CI gate in a few hundred lines that read the ledger directly.
Its parts map one to one onto Inspect's (dataset, solver, scorer), so moving
to Inspect later is mechanical. See docs/EVALS.md.
"""
