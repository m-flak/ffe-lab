"""Solvers: the thing being evaluated. Each turns one case into a validated verdict.

* ``ClaudeSolver``: the real agent. Its system prompt is the body of the
  agent's own definition in ``.claude/agents/<agent>.md``, so editing that file
  is exactly what the eval measures. The answer comes back as structured
  output (JSON constrained to a Pydantic model), never parsed from prose.
* ``HeuristicSolver``: a few regex rules on the asm. A floor: an LLM agent
  that can't beat it isn't earning its tokens. Deterministic and free, so it
  can always run.
* ``ReplaySolver``: re-scores answers recorded in an earlier report, for
  changing metrics without paying for new calls.

Eval answers are never written to the ledger: an eval is a measurement of the
agent, not evidence about the game.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Protocol

from pydantic import Field

from ..models import BlindLabelVerdict, Model, RedTeamVerdict
from .dataset import EvalCase

REPO = Path(__file__).resolve().parents[3]
AGENTS_DIR = REPO / ".claude" / "agents"
TASK_AGENT = {"blind_label": "blind-labeller", "red_team": "red-team"}

# USD per million tokens (input, output). Anthropic first-party list prices.
PRICES = {
    "claude-fable-5-1": (10.00, 50.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
DEFAULT_MODEL = "claude-opus-5-5"


class Usage(Model):
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0


class SolverOutput(Model):
    case_id: str
    verdict: BlindLabelVerdict | RedTeamVerdict | None = None
    error: str | None = None             # refusal, API error, invalid output: scored as wrong
    usage: Usage = Field(default_factory=Usage)


class Solver(Protocol):
    name: str
    model: str | None
    prompt_sha: str | None

    def solve(self, case: EvalCase) -> SolverOutput: ...


def verdict_type(task: str):
    return BlindLabelVerdict if task == "blind_label" else RedTeamVerdict


# ---------------------------------------------------------------- prompts
def load_agent_prompt(agent: str, path: str | Path | None = None) -> str:
    """Body of an agent definition, without its YAML frontmatter."""
    text = Path(path or AGENTS_DIR / f"{agent}.md").read_text()
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    return text.strip()


OFFLINE_NOTE = {
    "blind_label": (
        "This is an offline evaluation. There are no tools: the blind_view result for one "
        "function is in the user message. Do not call record_blind_label; return your "
        "answer in the required format instead."),
    "red_team": (
        "This is an offline evaluation of one round, before any experiment runs. There are "
        "no tools: the target claim, its suggested test and the blind_view result are in the "
        "user message. Decide whether you would attack, with which rival and prediction, and "
        "give your honest probability that the claim is wrong. Return it in the required format."),
}


def user_message(case: EvalCase) -> str:
    parts = []
    if case.claim is not None:
        parts.append("Target claim:\n" + json.dumps(case.claim.model_dump(), indent=1))
    parts.append("blind_view result:\n" + json.dumps(case.view, indent=1))
    return "\n\n".join(parts)


# ---------------------------------------------------------------- Claude
class ClaudeSolver:
    def __init__(self, task: str, model: str = DEFAULT_MODEL, prompt_path: str | Path | None = None,
                 effort: str | None = None, client=None):
        self.task, self.model, self.effort = task, model, effort
        self.system = load_agent_prompt(TASK_AGENT[task], prompt_path) + "\n\n" + OFFLINE_NOTE[task]
        self.prompt_sha = hashlib.sha256(self.system.encode()).hexdigest()[:12]
        self.name = f"claude:{model}" + (f"@{effort}" if effort else "")
        if client is None:
            import anthropic                               # optional dependency: pip install .[evals]
            client = anthropic.Anthropic()
        self.client = client
        try:
            from anthropic import APIError
            self._errors: tuple[type[Exception], ...] = (APIError, ValueError)
        except ImportError:                                # tests pass a fake client without the SDK
            self._errors = (ValueError,)

    def solve(self, case: EvalCase) -> SolverOutput:
        kwargs = {"output_config": {"effort": self.effort}} if self.effort else {}
        t0 = time.monotonic()
        try:
            resp = self.client.messages.parse(
                model=self.model, max_tokens=16000, system=self.system,
                messages=[{"role": "user", "content": user_message(case)}],
                output_format=verdict_type(self.task), **kwargs)
        except self._errors as e:  # ValueError: output failed validation
            return SolverOutput(case_id=case.id, error=f"{type(e).__name__}: {e}",
                                usage=Usage(seconds=time.monotonic() - t0))
        usage = self._usage(resp.usage, time.monotonic() - t0)
        if resp.stop_reason == "refusal":
            return SolverOutput(case_id=case.id, error="refusal", usage=usage)
        if resp.parsed_output is None:
            return SolverOutput(case_id=case.id, error=f"no parsed output (stop_reason={resp.stop_reason})",
                                usage=usage)
        return SolverOutput(case_id=case.id, verdict=resp.parsed_output, usage=usage)

    def _usage(self, u, seconds: float) -> Usage:
        # Cache reads/writes would be cheaper/dearer; evals send no cache_control, so plain rates apply.
        inp = (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) \
            + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        pin, pout = PRICES.get(self.model, (0.0, 0.0))
        return Usage(input_tokens=inp, output_tokens=u.output_tokens or 0, seconds=round(seconds, 2),
                     cost_usd=(inp * pin + (u.output_tokens or 0) * pout) / 1e6)


# ---------------------------------------------------------------- heuristic floor
HEURISTICS: list[tuple[str, str, float]] = [          # (regex on asm, label, confidence)
    (r"\b(out|in)\s+(dx|al|ax|0x[0-9a-f]+)|int\s+0x(10|33)\b", "platform", 0.55),
    (r"int\s+0x21\b", "io", 0.55),
    (r"\bf(ld|mul|add|sqrt|sin|cos|div)\b", "math", 0.45),
    (r"\b(shld|shrd|imul)\b", "physics", 0.35),
    (r"\brep\s+(movs|stos)[bdw]?\b", "render", 0.4),
    (r"\b(cmp|sub)\s+\w+,\s*0x(20|30|41|61)\b|'[A-Za-z]", "text", 0.4),
]


class HeuristicSolver:
    name = "heuristic"
    model = None
    prompt_sha = None

    def __init__(self, task: str):
        self.task = task

    def label(self, asm: str) -> tuple[str, float, str]:
        for pat, lab, conf in HEURISTICS:
            if re.search(pat, asm, re.IGNORECASE):
                return lab, conf, f"matched /{pat}/"
        return "unknown", 0.2, "no rule matched"

    def solve(self, case: EvalCase) -> SolverOutput:
        lab, conf, why = self.label(case.view.get("asm", ""))
        if self.task == "blind_label":
            v = BlindLabelVerdict(label=lab, confidence=conf, reasoning=why)
        else:
            claimed = case.claim.label.split("/")[0]
            disagree = lab not in (claimed, "unknown")
            v = RedTeamVerdict(p_claim_wrong=0.6 if disagree else 0.3, attack=disagree,
                               rival=lab if disagree else None,
                               rival_predicts=(case.claim.suggested_test.get("rival_predicts", "hit")
                                               if disagree else None),
                               reasoning=why)
        return SolverOutput(case_id=case.id, verdict=v)


# ---------------------------------------------------------------- replay
class ReplaySolver:
    def __init__(self, report_path: str | Path):
        from .runner import EvalReport
        rep = EvalReport.model_validate_json(Path(report_path).read_text())
        self.name, self.model, self.prompt_sha = f"replay:{rep.solver}", rep.model, rep.prompt_sha
        self.outputs = {r.output.case_id: r.output for r in rep.results}

    def solve(self, case: EvalCase) -> SolverOutput:
        return self.outputs.get(case.id) or SolverOutput(case_id=case.id, error="not in recorded report")
