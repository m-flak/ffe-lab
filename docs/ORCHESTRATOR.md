# Orchestrator

`ffelab agents run` runs the agent loop as a Python program instead of inside a
Claude Code chat. The program decides what happens each round, enforces the
budget and the ledger rules, and records every step as a trace. The LLM agents
("workers") are only asked to make judgement calls.

## Terms

- **Agent loop:** a model is given tools; it calls one, reads the result, calls
  another, and so on until it answers. The **Claude Agent SDK** (`claude-agent-sdk`)
  is Claude Code packaged as a Python library: `query()` runs that loop for you.
- **Orchestrator / worker:** the orchestrator is the code that hands out tasks;
  a worker is one agent-loop run on one task (label FUNC 412; look at claim bet 17).
- **Structured output:** the worker's final answer is JSON that the API
  constrains to a Pydantic model's schema (`BlindLabelVerdict`, `RedTeamPlan`),
  then validated. An invalid answer is a failed worker, never a stored guess.
- **In-process MCP tools:** the worker's tools (`blind_view`, `list_scenarios`,
  `ask_human`) are Python functions in this process, served to the SDK over MCP
  without starting a separate server.
- **Trace / span:** see Tracing below.

## What a round does

1. Every open contradiction is queued as a question for you (ledger rule:
   contradictions get reviewed by a person).
2. **Blind labellers**, in parallel: the most uncertain functions with no blind
   label yet. Each label is recorded as an ordinary claim (an opinion) and
   revealed against the other claims.
3. **Red team**, in parallel: the top claims from `attack_targets` that aren't
   already under attack. Each worker decides whether to attack, with which rival
   label, in which *registered* scenario, with what honest probability.
4. For each attack, one at a time (one emulator): the orchestrator places the
   rival bet and the prediction bet **first**, then runs the logging pass, which
   resolves the prediction and settles claims that became decisive.
5. Questions the workers asked through `ask_human` are queued.

Workers decide; the orchestrator acts. Workers' tools are read-only, so no agent
can place a bet after seeing a result, resolve a bet by hand or write a label
without validation. The agent prompts are the same `.claude/agents/*.md` files
the evals measure, plus a short note saying they run under the orchestrator.

## Budgets and stop conditions

| limit | flag | default |
|---|---|---|
| total spend | `--max-usd` | $5 |
| total tokens (in + out) | `--max-tokens` | none |
| wall-clock time | `--max-minutes` | 60 |
| rounds | `--rounds` | 10 |
| per-worker spend | `--worker-max-usd` | $0.50, or its share of what's left |
| per-worker agent turns | `--worker-max-turns` | 12 |
| questions waiting for you | `--max-questions` | 10 |

The run also stops when there is nothing to do, or after two rounds in a row
with no new labels, resolved bets or settled claims. Spend is what the SDK
reports per worker (`total_cost_usd`); a worker cut off by its own limit still
counts. Budget checks happen between rounds and before each worker starts, so a
run can overshoot by at most the workers already running.

## The question queue

Agents don't block on you. Questions pile up in `ffe.db` (`agent_questions`)
and the run stops once `--max-questions` are waiting, so you answer a batch:

```bash
ffelab agents questions                          # what's waiting
ffelab agents answer 7 "the combat save was taken mid-hyperspace" --close explained
ffelab agents answer 9 "it draws the scanner" --claim ui/scanner --confidence 0.7
ffelab agents dismiss 12
```

An answer is just text. `--claim` records your label as an ordinary claim (an
opinion that experiments still have to settle); `--close` closes the
contradiction the question was about. Answers are never shown to the workers:
their value is independence.

## Tracing

A **trace** is the record of one run, made of nested, timed **spans**:

```
agents.run
  round 1
    invoke_agent blind-labeller   (FUNC 412; tokens, cost, the verdict)
      chat claude-opus-5-5        (one model call: tokens in/out, text)
      execute_tool blind_view     (arguments, result, time)
    invoke_agent red-team
    experiment logging_pass       (scenario, experiment id, bets resolved)
```

Spans are OpenTelemetry (OTel), the vendor-neutral standard, so the viewer is
chosen with `--trace`:

| `--trace` | where | setup |
|---|---|---|
| `phoenix` | Arize Phoenix on your Mac | `pip install arize-phoenix && phoenix serve`, open http://localhost:6006 |
| `langfuse` | Langfuse cloud or self-hosted | `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` |
| `otlp` | any OTLP collector | `OTEL_EXPORTER_OTLP_ENDPOINT` (+ `_HEADERS`) |
| `console` | stderr | none |
| `none` | off (default) | |

Spans carry both the OTel GenAI attributes (`gen_ai.usage.input_tokens`, ...)
that Langfuse reads and the OpenInference ones (`openinference.span.kind`,
`llm.token_count.*`) that Phoenix reads. Why Phoenix is the default: DECISIONS D15.

## Running it

```bash
pip install -e ".[orchestrator]"     # claude-agent-sdk + OpenTelemetry

# free dry run: regex rules instead of Claude, fake game
ffelab agents run --worker heuristic --rounds 2

# the real thing, traced in Phoenix, $2 cap
phoenix serve &
ffelab agents run --backend qemu --max-usd 2 --trace phoenix --out reports/run1.json
```

Claude workers authenticate the way Claude Code does: `ANTHROPIC_API_KEY`, or a
logged-in Claude Code. The workers ignore your `~/.claude` settings, CLAUDE.md
files and other tools: each sees only its own three tools.

The default model is `claude-opus-5-5` (`--model`, `--effort` to change it). One
blind label with Opus 5.5 at low effort cost about $0.025 in a test run (3
turns, ~4.6k input tokens).

## Code

| file | what |
|---|---|
| `orchestrator/orchestrator.py` | the round loop, attacks, reports |
| `orchestrator/workers.py` | `ClaudeWorker` (Agent SDK), `HeuristicWorker`, tasks, `RedTeamPlan`, the tools |
| `orchestrator/budget.py` | `Budget`, `BudgetTracker`, stop reasons |
| `orchestrator/questions.py` | the queue table, contradictions, answers |
| `orchestrator/tracing.py` | OTel setup per viewer, span helpers |
| `harness/factory.py` | builds the fake or emulator backend (shared with the MCP server) |

`Orchestrator.run_round()` is one self-contained step, which is what a durable
workflow (PLAN step 4) would wrap as an activity.
