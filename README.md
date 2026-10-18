# ffelab

Evidence-driven, agent-assisted reverse engineering of **Frontier: First Encounters** (1995).

> New here? Start with **[GETTING_STARTED.md](GETTING_STARTED.md)**.
> Why things are the way they are: **[docs/DECISIONS.md](docs/DECISIONS.md)**.
> What's next: **[docs/PLAN.md](docs/PLAN.md)**.

FFE's x86 asm (from the [ffed3d](https://github.com/corpsofengineers/ffed3d)
repo) has ~1,600 functions, most without meaningful names. ffelab:

1. **Maps** them into the original source files, statically, from link order.
2. Lets **AI agents run experiments** on the real game in an emulator:
   logging passes, breakpoint halts, knockouts.
3. **Keeps score** of every guess (yours, the AI's, the red team's) so names
   rest on evidence, not on whoever sounded most confident.
4. **Writes confirmed names back** into the asm as alias labels, keeping it buildable.

```
static map → anchors + priors → experiments in the emulator → AI + your judgement → names in the asm
segment.py   tables, propagate   loop, knockout, harness       ledger, adversary     export.py
```

## Quick start (macOS or Linux)

```bash
git clone https://github.com/corpsofengineers/ffed3d ../ffed3d   # the asm source
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[agent,evals,orchestrator,dev]"

ffelab build --src ../ffed3d               # → ffe.db, data/module_map.json, docs/MODULE_MAP.md
ffelab exe-check path/to/FIRSTENC.EXE      # is your game exe the build the asm describes?
python -m pytest                            # 118 tests
FFED3D_DIR=../ffed3d python -m pytest       # also runs the real-data test
```

## Commands

| command | what it does |
|---|---|
| `ffelab build --src DIR` | Parse the asm, find module tables, segment into files, write `ffe.db` and the module map |
| `ffelab exe-check EXE [--map]` | Check an exe against the asm's layout; store a per-binary address map (identity, or aligned by content for other builds) |
| `ffelab export --src DIR [--auto]` | Write confirmed names into a copy of `ffe.asm` as alias labels, plus a reviewable diff |
| `ffelab uncertain` | The most uncertain functions, where the agent should look next |
| `ffelab embed` / `ffelab similar FUNC` | Embed every function's normalised code (and scenario fingerprints); list the functions most like one |
| `ffelab graph build / vote / outliers / check` | Call-graph structure: call layers, a spectral basis searchable with `ffelab similar --model callgraph-spectral-v1`, and static label votes from neighbours and the label quotient (see `docs/GRAPH.md`) |
| `ffelab db copy --from ffe.db --to postgresql:///ffelab` | Move the whole store to Postgres (or back to a file) |
| `ffelab eval run / gate / compare / export` | Score the blind labeller and red team against experiment-confirmed labels; gate prompt changes on it (see `docs/EVALS.md`) |
| `ffelab agents run [--worker claude\|heuristic] [--trace phoenix]` | Run the orchestrator: blind labeller and red team workers under a budget, every step traced (see `docs/ORCHESTRATOR.md`) |
| `ffelab agents questions / answer / dismiss` | The batched questions the agents and open contradictions have queued for you |
| `python -m ffelab.mcp_server` | The MCP server agents use (31 tools) |
| `python -m ffelab.mcp_server` | The MCP server agents use (34 tools) |

## Modules

| module | what it does |
|---|---|
| `asm.py` | Parses `ffe.asm`: functions, original `Pos` offsets, calls, DATA refs, prologue, alignment, filler |
| `tables.py` | Finds function-pointer tables in `ffedat.asm`, incl. the 12-slot **module event tables** |
| `segment.py` | Scores every gap between functions as a possible file boundary; learns weights from inherited names; event tables anchor modules |
| `models.py` | Pydantic models for every record (experiments, evidence, bets, contradictions, halts, tool inputs, agent verdicts): the shared schema |
| `ledger.py` | Beliefs over subsystem labels, Bayes updates, bets with Brier scoring, reliability per source and domain, contradictions, information gain |
| `labels.py` | Open hierarchical label paths (`render/planet/atmosphere/clouds`) and the growing vocabulary |
| `halt.py` | The rules that turn a halt into label likelihoods |
| `loop.py` | Logging passes and halts; records experiments, auto-resolves bets |
| `behavior.py` | Scenario tags, activity fingerprints, one-tag contrasts, behaviour groups |
| `propagate.py` | Label evidence for callers from their callees (experiments > beliefs > inherited names) |
| `graph.py` | Call-graph structure: spectral basis, call layers, label quotient; neighbour and quotient label evidence |
| `knockout.py` | Disable a function, diff screenshots against two baselines, record what vanished |
| `adversary.py` | Red team (attack targets, rival labels, attack bets) and blind second opinions |
| `exe.py` | Executable check and content-based address mapping between builds |
| `export.py` | Confirmed names → alias labels in a copy of the asm |
| `harness/` | `GameBackend` interface, `FakeBackend`, `EmulatorBackend` (QEMU / DOSBox-X), GDB client |
| `mcp_server.py` | The agent tool surface |
| `orchestrator/` | The agent loop as a program: Agent SDK workers, budgets and stop conditions, question queue, OpenTelemetry tracing |
| `evals/` | Eval harness: cases from settled labels, Claude / heuristic / replay solvers, metrics, gate |

Agents: `.claude/agents/red-team.md` and `.claude/agents/blind-labeller.md`
(Claude Code subagents, each limited to the tools its role needs).

## Results so far

See `docs/MODULE_MAP.md` for the full table.

- Function numbers follow original addresses almost perfectly (5 breaks, all
  JJFFE platform replacements): the list *is* the link order.
- **13 module event tables**; each module's handlers sit in one contiguous run,
  in the same order as the tables in the data segment.
- Segmenter: cross-validated **AUC 0.81** on held-out name pairs; named blocks
  (SysMenu, String/FF30, CdrFile, Int64, Tex, Mat) stay intact.
- Strongest boundary signal: calls staying on their own side of a cut. Nop
  padding does *not* mark file boundaries in this binary.
- Inherited names alone push label evidence into 428 functions via propagation.

## Ground rules the code enforces

- **Only experiments settle bets**, and only ones that ran after the bet and
  tested that function. Agents can't create experiment records directly.
- **Claims settle on experiment evidence alone**; agreeing opinions settle nothing.
- **Contradictions block naming** until closed as `explained` or `claim_withdrawn`.
- **Everyone is calibration-scored**, including you and the red team.
- **Agent output is validated** against Pydantic models before it touches the
  ledger: a confidence of 1.3 or a made-up label is rejected, not stored.

## Hooking up an agent

`.mcp.json` in the project root (use full paths):

```json
{
  "mcpServers": {
    "ffelab": {
      "command": "/full/path/to/ffe-lab/.venv/bin/python",
      "args": ["-m", "ffelab.mcp_server"],
      "env": {"FFELAB_DB": "/full/path/to/ffe-lab/ffe.db", "FFELAB_BACKEND": "fake"}
    }
  }
}
```

`FFELAB_DB` (here and for every `ffelab` command) can also be a Postgres URL such
as `postgresql:///ffelab`, so several agents can share one ledger; see
GETTING_STARTED 4.4.

For the real game set `FFELAB_BACKEND` to `qemu` or `dosbox-x` and `FFELAB_EMU`
to a JSON `EmulatorConfig`, e.g. `{"binary": "<id from exe-check>", "code_base": 0, "gdb_port": 1234}`.

## Status

- Working: static map, ledger, adversarial layer, fingerprints, propagation,
  knockouts, exe-check, export, Pydantic models, eval harness with a gate, Postgres + pgvector beside SQLite, call-graph structure, Agent SDK orchestrator with tracing. 118 tests (all also pass on Postgres), unit-tested against fakes plus one
  real-data test.
- **Not yet run against a live emulator.** Expect one session of tuning key maps,
  timings and the code base address.
- Not included: the FFE asm (clone ffed3d) and the game itself (bring your own copy).
