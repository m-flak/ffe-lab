"""Run a solver over a dataset, write a report, gate it against a baseline, compare runs."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pydantic import Field

from ..models import Model, Probability
from . import dataset as ds
from .metrics import CaseResult, Metrics, compute, grade
from .solvers import Solver


class EvalReport(Model):
    task: str
    dataset: str
    dataset_sha: str
    solver: str
    model: str | None = None
    prompt_sha: str | None = None        # changes whenever the agent's prompt changes
    created: datetime
    metrics: dict[str, Metrics]          # "all", "dev", "test"
    results: list[CaseResult]


def run(cases: list[ds.EvalCase], solver: Solver, dataset_name: str = "",
        split: str | None = None, progress=None) -> EvalReport:
    tasks = {c.task for c in cases}
    if len(tasks) != 1:
        raise ValueError(f"a dataset must hold one task, got {sorted(tasks)}")
    picked = [c for c in cases if split in (None, c.split)]
    results = []
    for i, c in enumerate(picked, 1):
        results.append(grade(c, solver.solve(c)))
        if progress:
            progress(i, len(picked), results[-1])
    metrics = {"all": compute(picked, results)}
    for s in ("dev", "test"):
        part = [r for r in results if r.split == s]
        if part and len(part) < len(results):
            metrics[s] = compute(picked, part)
    return EvalReport(task=tasks.pop(), dataset=dataset_name, dataset_sha=ds.digest(cases),
                      solver=solver.name, model=solver.model, prompt_sha=solver.prompt_sha,
                      created=datetime.now(timezone.utc), metrics=metrics, results=results)


# ---------------------------------------------------------------- gate
class Baseline(Model):
    """Thresholds a run must meet. Lives in evals/baselines/ and is reviewed like code."""
    task: str
    dataset: str
    split: str = "all"
    solver_prefix: str = ""              # e.g. "claude:" so a heuristic report can't pass a Claude gate
    min_accuracy: Probability | None = None
    max_brier: Probability | None = None
    max_ece: Probability | None = None
    max_error_rate: Probability | None = None
    max_cost_usd: float | None = None
    status: str = "measured"             # "provisional" until a real run sets the numbers
    note: str = ""
    source_report: dict = Field(default_factory=dict)


def gate(report: EvalReport, base: Baseline) -> list[str]:
    """Return the reasons this report fails the baseline (empty list: it passes)."""
    fails = []
    if report.task != base.task:
        fails.append(f"task {report.task} != baseline task {base.task}")
    if Path(report.dataset).name != Path(base.dataset).name:
        fails.append(f"dataset {report.dataset} != baseline dataset {base.dataset}")
    if not report.solver.startswith(base.solver_prefix):
        fails.append(f"solver {report.solver} is not a {base.solver_prefix}* solver")
    m = report.metrics.get(base.split)
    if m is None:
        return fails + [f"report has no '{base.split}' split"]
    checks = [("accuracy", m.accuracy, base.min_accuracy, ">="), ("brier", m.brier, base.max_brier, "<="),
              ("ece", m.ece, base.max_ece, "<="),
              ("error rate", m.errors / max(m.n, 1), base.max_error_rate, "<="),
              ("cost", m.cost_usd, base.max_cost_usd, "<=")]
    for name, val, lim, op in checks:
        if lim is None:
            continue
        ok = val >= lim if op == ">=" else val <= lim
        if not ok:
            fails.append(f"{name} {val:.4f} is not {op} {lim:.4f}")
    return fails


def baseline_from(report: EvalReport, split: str = "all", margin: float = 0.05,
                  solver_prefix: str | None = None) -> Baseline:
    """Turn a measured run into thresholds, with a margin so ordinary run-to-run noise passes."""
    m = report.metrics[split]
    return Baseline(task=report.task, dataset=report.dataset, split=split,
                    solver_prefix=solver_prefix if solver_prefix is not None else report.solver.split(":")[0],
                    min_accuracy=max(0.0, round(m.accuracy - margin, 4)),
                    max_brier=min(1.0, round(m.brier + margin, 4)),
                    max_error_rate=min(1.0, round(m.errors / max(m.n, 1) + margin, 4)),
                    note=f"from {report.solver} on {report.created:%Y-%m-%d}, margin {margin}",
                    source_report={"solver": report.solver, "model": report.model,
                                   "prompt_sha": report.prompt_sha, "dataset_sha": report.dataset_sha})


def compare(reports: list[EvalReport], split: str = "all") -> str:
    """Side-by-side table of several runs (models, prompts, solvers)."""
    cols = ["accuracy", "accuracy_ci95", "brier", "ece", "overconfidence", "errors", "cost_usd",
            "cost_per_correct_usd"]
    head = "| solver | prompt | " + " | ".join(cols) + " |"
    lines = [head, "|" + "---|" * (len(cols) + 2)]
    for r in reports:
        m = r.metrics.get(split)
        if m is None:
            continue
        vals = [getattr(m, c) for c in cols]
        lines.append(f"| {r.solver} | {r.prompt_sha or '-'} | " + " | ".join(
            "-" if v is None else (f"{v[0]:.2f}-{v[1]:.2f}" if isinstance(v, tuple) else
                                   f"{v:.4f}" if isinstance(v, float) else str(v)) for v in vals) + " |")
    subs = sorted({g for r in reports if split in r.metrics for g in r.metrics[split].by_subsystem})
    if subs:
        lines += ["", "Accuracy by subsystem:", "",
                  "| solver | " + " | ".join(subs) + " |", "|" + "---|" * (len(subs) + 1)]
        for r in reports:
            if split in r.metrics:
                bs = r.metrics[split].by_subsystem
                lines.append(f"| {r.solver} | " + " | ".join(
                    f"{bs[g]['accuracy']:.2f} (n={int(bs[g]['n'])})" if g in bs else "-" for g in subs) + " |")
    return "\n".join(lines)
