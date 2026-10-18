"""Workers: the LLM agents the orchestrator hands one task at a time.

Two kinds, the same two agents the evals measure (``.claude/agents/``):

* **blind labeller**: labels one function from ``blind_view`` only.
* **red team**: looks at one confident claim and decides whether to attack it,
  with which rival label, in which registered scenario.

A worker *decides*; the orchestrator *acts*. Workers get read-only tools
(``blind_view``, ``list_scenarios``, and once their modules are installed
``similar_functions`` and, for the red team only, ``quotient_outliers``) plus
``ask_human``, and answer with a
Pydantic model as structured output (JSON the API constrains to the model's
schema). Writing to the ledger, placing bets and running experiments stay in
ordinary code, so the ledger rules (bets placed before the run, only
experiments resolve them) can't be skipped by an agent having a bad day.

``ClaudeWorker`` runs each task as one Claude Agent SDK ``query()``: the SDK
runs the agent loop (model call, tool call, model call, ...) inside Claude
Code, with this process's tools served in-process over MCP. ``HeuristicWorker``
is the free, deterministic stand-in (the eval floor's regex rules) for dry runs
and tests.
"""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import Field, ValidationError, model_validator

from .. import adversary as adv
from .. import behavior
from ..evals.solvers import DEFAULT_MODEL, HeuristicSolver, load_agent_prompt
from ..ledger import Ledger
from ..models import BlindLabelVerdict, FuncNum, Model, RedTeamVerdict
from . import tracing
from .questions import QuestionDraft

WorkerKind = Literal["blind-labeller", "red-team"]


# ---------------------------------------------------------------- tasks and answers
class BlindTask(Model):
    kind: Literal["blind-labeller"] = "blind-labeller"
    func: FuncNum

    @property
    def id(self) -> str:
        return f"FUNC {self.func}"


class RedTeamTask(Model):
    kind: Literal["red-team"] = "red-team"
    target: dict[str, Any]                    # one row of adversary.attack_targets
    scenarios: dict[str, list[str]]           # registered save states and their tags

    @property
    def func(self) -> int:
        return self.target["func"]

    @property
    def id(self) -> str:
        return f"claim bet {self.target['claim_bet']} (FUNC {self.func})"


Task = BlindTask | RedTeamTask


class RedTeamPlan(RedTeamVerdict):
    """The red team's verdict plus where to test it. ``p_claim_wrong`` is the attack's probability."""
    scenario: str | None = Field(None, description="A registered scenario name to run, if attacking.")

    @model_validator(mode="after")
    def _attack_needs_scenario(self):
        if self.attack and not self.scenario:
            raise ValueError("an attack needs a registered scenario to run")
        return self


class WorkerUsage(Model):
    input_tokens: int = 0                     # includes cache reads and writes
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0
    seconds: float = 0.0


class WorkerResult(Model):
    worker: str
    kind: WorkerKind
    task: str
    output: BlindLabelVerdict | RedTeamPlan | None = None
    error: str | None = None                  # refusal, budget/turn limit, invalid answer, crash
    usage: WorkerUsage = Field(default_factory=WorkerUsage)
    questions: list[QuestionDraft] = Field(default_factory=list)
    tool_calls: list[str] = Field(default_factory=list)


class Worker(Protocol):
    name: str
    model: str | None

    async def run(self, task: Task, max_turns: int, max_usd: float | None, parent=None) -> WorkerResult: ...


OUTPUT = {"blind-labeller": BlindLabelVerdict, "red-team": RedTeamPlan}


# ---------------------------------------------------------------- prompts
ORCHESTRATOR_NOTE = {
    "blind-labeller": (
        "You are running as a worker under the ffelab orchestrator. Your tools are {tools}. "
        "Do not call record_blind_label: return your answer in the required output "
        "format and the orchestrator records it for you. If something can only be settled by a "
        "person, call ask_human; the question is queued for them, and you still give your best "
        "answer now."),
    "red-team": (
        "You are running as a worker under the ffelab orchestrator, and this round you only decide; "
        "you do not run anything. Your tools are {tools}; the "
        "target claim and the registered scenarios are in the user message. Do not call "
        "red_team_targets, place_attack or run_logging_pass. If you attack, give the rival label, "
        "a scenario from the registered list, what the rival predicts there (hit or miss), and your "
        "honest probability that the claim is wrong. The orchestrator places that as a bet before "
        "running the scenario, and the run resolves it. If no registered scenario separates claim "
        "from rival, don't attack; you may ask_human for the save state you would need."),
}
def system_prompt(kind: WorkerKind, prompt_path: str | Path | None = None) -> str:
    # The agent files explain each tool; the note says which ones this worker actually has.
    note = ORCHESTRATOR_NOTE[kind].replace("{tools}", ", ".join(tool_names(kind)))
    return load_agent_prompt(kind, prompt_path) + "\n\n" + note


def user_message(task: Task) -> str:
    if isinstance(task, BlindTask):
        return f"Label FUNC {task.func}. Start with blind_view({task.func})."
    scen = "\n".join(f"- {name}: {', '.join(tags) or '(no tags)'}" for name, tags in sorted(task.scenarios.items()))
    return ("Target claim:\n" + json.dumps(task.target, indent=1) +
            "\n\nRegistered scenarios (name: tags):\n" + (scen or "(none registered)"))


# ---------------------------------------------------------------- tools (read-only + ask_human)
TOOLS = {"blind-labeller": ("blind_view", "similar_functions", "ask_human"),
         "red-team": ("blind_view", "list_scenarios", "similar_functions", "quotient_outliers", "ask_human")}
# Tools backed by modules that arrive with other plan items (embed.py with Postgres/pgvector,
# graph.py with the call-graph work). Each registers only when its module is installed.
# graph_votes is never a worker tool: it writes evidence, and workers are read-only.
NEEDS = {"similar_functions": "ffelab.embed", "quotient_outliers": "ffelab.graph"}


def _have(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        return False


def tool_names(kind: WorkerKind) -> list[str]:
    return [t for t in TOOLS[kind] if t not in NEEDS or _have(NEEDS[t])]


def build_tools(ledger: Ledger, kind: WorkerKind, result: WorkerResult, parent=None) -> list:
    """The worker's tools as Agent SDK in-process MCP tools. Each call is traced under ``parent``."""
    from claude_agent_sdk import tool

    def text(v) -> dict:
        return {"content": [{"type": "text", "text": v if isinstance(v, str) else json.dumps(v)}]}

    def traced(name, fn):
        async def handler(args):
            result.tool_calls.append(name)
            with tracing.span(f"execute_tool {name}", tracing.tool_attrs(name, args), parent) as s:
                try:
                    out = fn(args)
                except (KeyError, ValueError, ValidationError) as e:
                    tracing.mark_error(s, str(e))
                    return {**text(f"error: {e}"), "is_error": True}
                s.set_attribute("output.value", tracing._clean(out))
                return text(out)
        return handler

    def blind_view(a):
        return adv.blind_view(ledger, int(a["func"]))

    def list_scenarios(a):
        return {k: sorted(v) for k, v in behavior.scenarios(ledger).items()}

    def ask_human(a):
        d = QuestionDraft(question=a["question"], func=a.get("func"))
        result.questions.append(d)
        return "queued for the human; carry on with your best answer"

    def similar_functions(a):
        embed = importlib.import_module(NEEDS["similar_functions"])
        model = a.get("model") or embed.DEFAULT_MODEL
        return embed.similar(ledger.con, int(a["func"]), model, int(a.get("k", 10)))

    def quotient_outliers(a):
        graph = importlib.import_module(NEEDS["quotient_outliers"])
        out = graph.outliers(ledger)
        if a.get("func") is not None:
            out = [o for o in out if int(a["func"]) in (o["caller"], o["callee"])]
        return out

    defs = {
        "blind_view": ("A function's code and structure with no beliefs, claims or inherited names.",
                       {"type": "object", "properties": {"func": {"type": "integer"}}, "required": ["func"]},
                       blind_view),
        "list_scenarios": ("All registered save-state scenarios and their tags.",
                           {"type": "object", "properties": {}}, list_scenarios),
        "ask_human": ("Queue one specific question for the human. Non-blocking: answers come in a later "
                      "batch, so give your best answer now anyway.",
                      {"type": "object", "properties": {"question": {"type": "string"},
                                                        "func": {"type": "integer"}},
                       "required": ["question"]}, ask_human),
        "similar_functions": (
            "Functions most like func: by instruction patterns (model asm-ngram-v1) or by position in "
            "the call graph (callgraph-spectral-v1). Numbers and cosine scores only, no names, labels "
            "or beliefs. A hint for where to look, never evidence.",
            {"type": "object", "properties": {
                "func": {"type": "integer"}, "k": {"type": "integer", "minimum": 1, "maximum": 50},
                "model": {"type": "string", "enum": ["asm-ngram-v1", "callgraph-spectral-v1"]}},
             "required": ["func"]}, similar_functions),
        "quotient_outliers": (
            "Calls between two labelled functions that no other labelled call supports (a mislabel or a "
            "real cross-layer call). Optionally only those involving func. Leads, not verdicts.",
            {"type": "object", "properties": {"func": {"type": "integer"}}}, quotient_outliers),
    }
    return [tool(n, defs[n][0], defs[n][1])(traced(n, defs[n][2])) for n in tool_names(kind)]


# ---------------------------------------------------------------- Claude (Agent SDK)
class ClaudeWorker:
    """One Agent SDK ``query()`` per task, isolated from the user's Claude Code settings."""

    def __init__(self, ledger: Ledger, model: str = DEFAULT_MODEL, effort: str | None = None,
                 prompts: dict[str, Path] | None = None, query_fn=None):
        self.ledger, self.model, self.effort = ledger, model, effort
        self.name = f"claude:{model}" + (f"@{effort}" if effort else "")
        self.prompts = {k: system_prompt(k, (prompts or {}).get(k)) for k in OUTPUT}
        if query_fn is None:
            from claude_agent_sdk import query as query_fn   # optional dependency: .[orchestrator]
        self.query = query_fn

    def options(self, kind: WorkerKind, result: WorkerResult, max_turns: int, max_usd: float | None, parent):
        from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server
        server = create_sdk_mcp_server("ffelab", tools=build_tools(self.ledger, kind, result, parent))
        return ClaudeAgentOptions(
            system_prompt=self.prompts[kind],
            mcp_servers={"ffelab": server},
            tools=[],                                             # no built-in Bash/Read/Edit/...
            allowed_tools=[f"mcp__ffelab__{t}" for t in tool_names(kind)],
            permission_mode="dontAsk",                            # anything not allowed is denied
            setting_sources=[],                                   # ignore ~/.claude and project settings
            model=self.model, effort=self.effort,
            max_turns=max_turns, max_budget_usd=max_usd,
            output_format={"type": "json_schema", "schema": OUTPUT[kind].model_json_schema()},
        )

    async def run(self, task: Task, max_turns: int, max_usd: float | None, parent=None) -> WorkerResult:
        from claude_agent_sdk import AssistantMessage, ClaudeSDKError, ResultMessage, TextBlock, ToolUseBlock
        res = WorkerResult(worker=self.name, kind=task.kind, task=task.id)
        t0 = time.monotonic()
        with tracing.span(f"invoke_agent {task.kind}", tracing.agent_attrs(task.kind, self.model, task.id),
                          parent) as s:
            final: ResultMessage | None = None
            chat, chat_id = None, None
            try:
                async for msg in self.query(prompt=user_message(task),
                                            options=self.options(task.kind, res, max_turns, max_usd, s)):
                    if isinstance(msg, AssistantMessage):
                        # Claude Code sends one AssistantMessage per content block; blocks of the
                        # same model call share message_id, so they make one "chat" span.
                        if msg.message_id != chat_id or chat is None:
                            if chat is not None:
                                chat.end()
                            chat_id = msg.message_id
                            chat = tracing.start(f"chat {msg.model}", {"gen_ai.request.model": msg.model}, s)
                            texts, calls = [], []
                        texts += [b.text for b in msg.content if isinstance(b, TextBlock)]
                        calls += [b.name for b in msg.content if isinstance(b, ToolUseBlock)]
                        u = msg.usage or {}
                        tracing.set_attrs(chat, tracing.chat_attrs(
                            msg.model, _input_tokens(u), u.get("output_tokens", 0) or 0,
                            u.get("cache_read_input_tokens", 0) or 0, u.get("cache_creation_input_tokens", 0) or 0,
                            "\n".join(texts), calls))
                    elif isinstance(msg, ResultMessage):
                        final = msg
            except ClaudeSDKError as e:                  # error results (turn/budget limits) raise after the result
                if final is None or not final.is_error:
                    res.error = f"{type(e).__name__}: {e}"
            finally:
                if chat is not None:
                    chat.end()
            res.usage.seconds = round(time.monotonic() - t0, 2)
            if final is not None:
                self._finish(res, final, task.kind)
            elif res.error is None:
                res.error = "no result from the agent"
            tracing.set_attrs(s, {**tracing.usage_attrs(res.usage.input_tokens, res.usage.output_tokens,
                                                         res.usage.cost_usd),
                                  "ffelab.turns": res.usage.turns,
                                  "output.value": res.output.model_dump() if res.output else res.error})
            if res.error:
                tracing.mark_error(s, res.error)
        return res

    def _finish(self, res: WorkerResult, final, kind: WorkerKind) -> None:
        u = final.usage or {}
        res.usage.input_tokens = _input_tokens(u)
        res.usage.output_tokens = u.get("output_tokens", 0) or 0
        res.usage.cache_read_tokens = u.get("cache_read_input_tokens", 0) or 0
        res.usage.cache_write_tokens = u.get("cache_creation_input_tokens", 0) or 0
        res.usage.cost_usd = final.total_cost_usd or 0.0
        res.usage.turns = final.num_turns
        if final.is_error or final.subtype != "success":
            res.error = f"{final.subtype}: " + "; ".join(final.errors or [final.result or ""])
            return
        if final.stop_reason == "refusal":
            res.error = "refusal"
            return
        if final.structured_output is None:
            res.error = "no structured output"
            return
        try:
            res.output = OUTPUT[kind].model_validate(final.structured_output)
        except ValidationError as e:
            res.error = f"invalid answer: {e.errors()[0]['msg']}"


def _input_tokens(u: dict) -> int:
    return sum(u.get(k, 0) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


# ---------------------------------------------------------------- heuristic (free, deterministic)
class HeuristicWorker:
    """The eval floor as a worker: regex rules on the asm, no model, no cost."""
    name = "heuristic"
    model = None

    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self.rules = HeuristicSolver("blind_label")

    async def run(self, task: Task, max_turns: int, max_usd: float | None, parent=None) -> WorkerResult:
        res = WorkerResult(worker=self.name, kind=task.kind, task=task.id)
        with tracing.span(f"invoke_agent {task.kind}", tracing.agent_attrs(task.kind, None, task.id), parent) as s:
            view = adv.blind_view(self.ledger, task.func)
            lab, conf, why = self.rules.label(view.get("asm", ""))
            if isinstance(task, BlindTask):
                res.output = BlindLabelVerdict(label=lab, confidence=conf, reasoning=why)
            else:
                test = task.target["test"]
                rival = task.target["rival"]
                scen = next((n for n, tags in sorted(task.scenarios.items()) if test["context"] in tags), None)
                attack = scen is not None and lab == rival
                res.output = RedTeamPlan(p_claim_wrong=0.6 if attack else 0.3, attack=attack,
                                         rival=rival if attack else None,
                                         rival_predicts=test["rival_predicts"] if attack else None,
                                         scenario=scen if attack else None, reasoning=why)
            tracing.set_attrs(s, {"output.value": res.output.model_dump()})
        return res
