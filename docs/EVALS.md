# Evals

How ffelab measures its own agents. An **eval** is a fixed set of questions with
known answers, run against an agent so a change (new prompt, new model) can be
compared with the last one by a number instead of by feel.

## What is measured

| agent | question per case | graded against |
|---|---|---|
| blind labeller | "what subsystem is this function?" from `blind_view` only | the label experiments settled |
| red team | "is this claim wrong, would you attack it, with which rival?" | whether experiments refuted the claim |

The red-team eval is a single round **before** any experiment runs (the decision
to attack, the rival and the probability). The full loop, where it also picks a
scenario and runs it, needs the emulator and comes with the orchestrator (PLAN 2).

Metrics (definitions in `src/ffelab/evals/metrics.py`):

- **accuracy**, overall and per subsystem, with a bootstrap 95% interval (how
  much of a difference could be luck);
- **Brier score** and **ECE** (calibration: does "80% sure" mean right 80% of the time);
- **overconfidence**: mean stated probability minus how often it happened;
- red team: **refute precision / recall** (attacks that hit wrong claims, wrong
  claims that got attacked) and **rival accuracy**;
- **tokens, cost, cost per correct label**, so models can be compared on price too.

## Where answers come from

Only experiments make ground truth (DECISIONS D3, D11). `ffelab eval export`
takes a function as a case only when its experiment-only belief is decisive (the
same test `settle_claims` uses) and takes red-team cases only from settled
claims. Opinions, inherited names and propagated labels never become gold.
Eval answers are never written back to the ledger.

Each case freezes the `blind_view` it was exported with, so evals run without
`ffe.db` or the asm, and carries a `split`:

- **dev**: look at these answers, tune prompts on them;
- **test** (about 30%, picked by a stable hash): held out. Read only the score.
  Looking at test answers while editing a prompt slowly fits the prompt to them
  and the score stops predicting anything.

## The datasets today

The emulator hasn't run yet, so no label is experiment-settled. Until it is,
`evals/datasets/smoke_*.jsonl` hold **synthetic** cases: 14 hand-written
FFE-style functions (10 of them also as red-team targets, half with wrong
claims) whose label is known because they were written that way
(`evals/smoke/make_smoke.py`). They test the harness and catch gross prompt
breakage. They are small and easy, so treat their scores as a smoke test, not
as how well the agents do on the game.

## Commands

```bash
pip install -e ".[evals]"            # the Anthropic SDK; set ANTHROPIC_API_KEY

# score the blind labeller (prompt = .claude/agents/blind-labeller.md)
ffelab eval run --dataset evals/datasets/smoke_blind.jsonl --out reports/opus.json

# compare models or prompts
ffelab eval run --dataset evals/datasets/smoke_blind.jsonl --model claude-haiku-5-5 --out reports/haiku.json
ffelab eval run --dataset evals/datasets/smoke_blind.jsonl --prompt my-draft.md --out reports/draft.json
ffelab eval compare reports/opus.json reports/haiku.json reports/draft.json

# free: the regex floor, and re-scoring a recorded run without new calls
ffelab eval run --dataset evals/datasets/smoke_blind.jsonl --solver heuristic
ffelab eval run --dataset evals/datasets/smoke_blind.jsonl --solver replay --replay reports/opus.json

# gate, and set a new baseline from a run you trust
ffelab eval gate --report reports/opus.json --baseline evals/baselines/blind-labeller.json
ffelab eval baseline --report reports/opus.json --out evals/baselines/blind-labeller.json --margin 0.05

# once experiments settle labels: real cases
ffelab eval export --db ffe.db --out evals/datasets
```

The default model is `claude-opus-5-5`. The solver sends the agent's own
definition file as the system prompt, plus a note that tools are replaced by
the case data, and asks for structured output: the API constrains the answer
to the `BlindLabelVerdict` / `RedTeamVerdict` JSON schema and the SDK validates
it into the Pydantic model. A refusal or invalid answer counts as wrong.
Server-side model fallbacks are off on purpose: a silent switch to another
model would make the report describe the wrong model.

## CI

- `ci.yml`, every push and PR: unit tests, then the harness end to end with the
  heuristic floor against `evals/baselines/heuristic-*.json` (free, deterministic).
- `evals.yml`, PRs that touch `.claude/agents/`, the eval code or `evals/`, and
  manual runs (pick the model): runs both agents with Claude and fails the PR
  if they fall below `evals/baselines/blind-labeller.json` / `red-team.json`.
  Needs the `ANTHROPIC_API_KEY` repository secret; without it the job warns and
  skips. Not measured yet; the estimate is under $1 per smoke set on Opus 5.5, most of it thinking tokens.

The Claude baselines are marked **provisional**: no live run has set them yet.
Until one does they only require matching the heuristic floor's accuracy, a
Brier better than a coin flip (0.25), under 10% failed calls and under $2 a run.
After the first green live run, download its report artifact and run
`ffelab eval baseline` to replace them with measured numbers (minus a margin for
run-to-run noise). With real data, gate on the `test` split.
