# ffelab

Evidence-driven, agent-assisted reverse engineering of **Frontier: First Encounters** (1995).

> New here? Start with **[GETTING_STARTED.md](GETTING_STARTED.md)**.
> How to use it day to day, in order: **[docs/USAGE.md](docs/USAGE.md)**.
> Why things are the way they are: **[docs/DECISIONS.md](docs/DECISIONS.md)**.
> What's next: **[docs/PLAN.md](docs/PLAN.md)**.
> Game art (bitmaps, meshes): **[docs/ASSET_FORMATS.md](docs/ASSET_FORMATS.md)**.
> Running the game in QEMU: **[docs/EMULATOR.md](docs/EMULATOR.md)**.
> Static research by an outside agent (Muse): **[docs/STATIC_RESEARCH.md](docs/STATIC_RESEARCH.md)**.
> Frontier: Elite II as a reference: **[docs/REFERENCES.md](docs/REFERENCES.md)**.
> Weaving FFE into the Zig engine: **[docs/STATIC_RESEARCH.md](docs/STATIC_RESEARCH.md#frankensteining-the-zig-engine)**.

FFE's x86 asm (from the [ffed3d](https://github.com/corpsofengineers/ffed3d)
repo) has ~1,600 functions, most without meaningful names. ffelab:

1. **Maps** them into the original source files, statically, from link order.
2. Lets **AI agents run experiments** on the real game in an emulator:
   logging passes, breakpoint halts, knockouts (judged by screen and sound),
   memory watchpoints, and DOS file calls.
3. **Keeps score** of every guess (yours, the AI's, the red team's) so names
   rest on evidence, not on whoever sounded most confident.
4. **Writes confirmed names back** into the asm as alias labels, keeping it buildable.
5. **Helps plan the new engine**: a researcher reads the sokol-zig engine next
   to FFE, checks what gutting a function would cost, and proposes how the two
   connect. Those links are design proposals, never evidence (D19).

```
static map → anchors + priors → experiments in the emulator → AI + your judgement → names in the asm
segment.py   tables, propagate   loop, knockout, harness       ledger, adversary     export.py
```

## Quick start (macOS or Linux)

```bash
git clone https://github.com/corpsofengineers/ffed3d ../ffed3d   # the asm source
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[agent,evals,orchestrator,pg,durable,dev]"   # or just .[agent,evals,dev] for the core

ffelab build --src ../ffed3d               # → ffe.db, data/module_map.json, docs/MODULE_MAP.md
ffelab exe-check path/to/FIRSTENC.EXE      # is your game exe the build the asm describes?
python -m pytest
FFED3D_DIR=../ffed3d python -m pytest       # also runs the real-data test
```

## Commands

| command | what it does |
|---|---|
| `ffelab build --src DIR` | Parse the asm, find module tables, segment into files, write `ffe.db` and the module map |
| `ffelab exe-check EXE [--map]` | Check an exe against the asm's layout; store a per-binary address map (identity, or aligned by content for other builds) |
| `ffelab export --src DIR [--auto]` | Write confirmed names into a copy of `ffe.asm` as alias labels, plus a reviewable diff |
| `ffelab uncertain` | The most uncertain functions, where the agent should look next |
| `ffelab emu calibrate --exe EXE` / `ffelab emu monitor CMD` | Find `code_base` in the running game and print `FFELAB_EMU`; send one QEMU monitor command (see `docs/EMULATOR.md`) |
| `ffelab scenario save / tag / list` | Save a QEMU snapshot and tag it as a scenario in one go; retag; list scenarios and whether each snapshot exists |
| `ffelab research view / refs / grep / claim / import / retag` | Static research for an outside agent: read freely, write claims only (see `docs/STATIC_RESEARCH.md`) |
| `ffelab research subsystem SPEC / cut-check FUNC / seam [SYMBOL]` | FFE as parts of the new engine: a subsystem as a box to cut out (entry points, outside calls, shared data); what gutting one function would cost (verdict gut / split / keep / unsure, a prediction a knockout settles); the asm's calls out to ffed3d's C/C++ (`--ffed3d DIR` or `FFELAB_FFED3D_ROOT`) |
| `ffelab research link FFE ZIG RELATION --note TEXT` | Propose how an FFE function, data label, segment or label meets the Zig engine (`replaces`, `wraps`, `stubs`, `calls_into`, `reads`, `mirrors`); needs `--source` or `FFELAB_SOURCE`; `--zig-root DIR` or `FFELAB_ZIG_ROOT` also reports whether the Zig file exists yet |
| `ffelab bridge list [--ffe REF] [--zig PATH] [--status S]` / `ffelab bridge status ID STATUS` | The FFE <-> Zig links proposed so far; your call on one (`accepted`, `in_progress`, `done`, `rejected`) |
| `ffelab ref import --fe2 fe2.s --src DIR` / `ffelab ref show [FUNC]` | Match FFE functions to Frontier: Elite II (glFrontier's `fe2.s`); reference facts, not evidence (see `docs/REFERENCES.md`) |
| `ffelab embed` / `ffelab similar FUNC` | Embed every function's normalised code (and scenario fingerprints); list the functions most like one |
| `ffelab graph build / vote / outliers / check` | Call-graph structure: call layers, a spectral basis searchable with `ffelab similar --model callgraph-spectral-v1`, and static label votes from neighbours and the label quotient (see `docs/GRAPH.md`) |
| `ffelab db copy --from ffe.db --to postgresql:///ffelab` | Move a whole store into an empty one: SQLite to Postgres (or back). Then `ffelab embed` and `ffelab graph build`; see GETTING_STARTED 4.4 |
| `ffelab eval export / run / baseline / gate / compare` | Score the blind labeller and red team against experiment-confirmed labels; gate prompt changes on it (see `docs/EVALS.md`) |
| `ffelab agents run [--worker claude\|heuristic] [--trace phoenix]` | Run the orchestrator: blind labeller and red team workers under a budget, every step traced (see `docs/ORCHESTRATOR.md`) |
| `ffelab agents questions / answer / dismiss` | The batched questions the agents and open contradictions have queued for you |
| `python -m ffelab.workflows worker / start / status / pause / resume / stop` | Durable experiment campaigns on Temporal: a crash or hang retries from the save state (see GETTING_STARTED "Long runs") |
| `python -m ffelab.mcp_server` | The MCP server agents use |
| `python -m ffelab.mcp_research` | The MCP server for an outside researcher (`ffelab-research`): read tools, `subsystem`, `cut_check`, `seam`, the Zig engine tools, and the only writes are `claim` and `link` |
| `python3 tools/meshwalk.py all --src DIR` | Dump the game's meshes as OBJ files (stdlib only; see `docs/ASSET_FORMATS.md`) |
| `python3 tools/texdump.py all --src DIR` | Dump the game's bitmaps/textures as PNG files (stdlib only; see `docs/ASSET_FORMATS.md`) |

## Modules

| module | what it does |
|---|---|
| `asm.py` | Parses `ffe.asm`: functions, original `Pos` offsets, calls, DATA refs, prologue, alignment, filler |
| `tables.py` | Finds function-pointer tables in `ffedat.asm`, incl. the 12-slot **module event tables** |
| `segment.py` | Scores every gap between functions as a possible file boundary; learns weights from inherited names; event tables anchor modules |
| `models.py` | Pydantic models for every record (experiments, evidence, bets, contradictions, halts, tool inputs, agent verdicts): the shared schema |
| `db.py`, `pg.py` | The store: one schema for SQLite and Postgres (created or migrated on connect, so an old `ffe.db` gains new tables such as `bridges` the first time it's opened), and the thin Postgres adapter |
| `ledger.py` | Beliefs over subsystem labels, Bayes updates, bets with Brier scoring, reliability per source and domain, contradictions, information gain |
| `labels.py` | Open hierarchical label paths (`render/planet/atmosphere/clouds`) and the growing vocabulary |
| `halt.py` | The rules that turn a halt into label likelihoods |
| `loop.py` | Logging passes and halts; records experiments, auto-resolves bets |
| `behavior.py` | Scenario tags, activity fingerprints, one-tag contrasts, behaviour groups |
| `propagate.py` | Label evidence for callers from their callees (experiments > beliefs > inherited names) |
| `graph.py` | Call-graph structure: spectral basis, call layers, label quotient; neighbour and quotient label evidence |
| `knockout.py` | Disable a function, diff screenshots and sound-device write counts against two baselines, record what vanished |
| `observe.py` | Watch passes (memory watchpoints: who touches what) and file-access passes (which function opens which file via DOS `int 21h`) |
| `adversary.py` | Red team (attack targets, rival labels, attack bets) and blind second opinions |
| `exe.py` | Executable check and content-based address mapping between builds |
| `export.py` | Confirmed names → alias labels in a copy of the asm |
| `research.py`, `mcp_research.py` | Static research: read views and claim writes, as CLI and as an MCP server (which also serves the `bridge.py` tools and `link`) |
| `bridge.py` | The Zig side: read-only access to the engine repo, `subsystem`, `cut_check`, `seam`, and FFE <-> Zig links (the `bridges` table, outside the ledger) |
| `fe2ref.py` | Matches against Frontier: Elite II's `fe2.s` (data tables, constants, call graph) into `ref_matches` |
| `embed.py` | Local, deterministic embeddings (code n-grams, scenario fingerprints) and similarity search |
| `harness/` | `GameBackend` interface, `FakeBackend`, `EmulatorBackend` (QEMU / DOSBox-X) with watchpoints and sound tracing, GDB client, `factory.py` (backend from a name), `bringup.py` (code_base calibration, monitor, snapshots) |
| `mcp_server.py` | The agent tool surface |
| `mcp_thread.py` | Runs every MCP tool call on one thread, so a SQLite connection and the emulator are only ever touched from one place |
| `cli.py` | The `ffelab` command |
| `orchestrator/` | The agent loop as a program: Agent SDK workers, budgets and stop conditions, question queue, OpenTelemetry tracing |
| `workflows/` | Temporal campaigns: the campaign workflow, retry-safe experiment activities, the command line |
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

An outside researcher gets its own server instead, which can't run experiments
or bets. `FFELAB_SOURCE` is required and fixes whose claims these are; the two
roots are optional and switch on the Zig tools and `seam`:

```json
{
  "mcpServers": {
    "ffelab-research": {
      "command": "/full/path/to/ffe-lab/.venv/bin/python",
      "args": ["-m", "ffelab.mcp_research"],
      "env": {"FFELAB_DB": "/full/path/to/ffe-lab/ffe.db", "FFELAB_SOURCE": "ai:muse",
              "FFELAB_ZIG_ROOT": "/full/path/to/zig-engine",
              "FFELAB_FFED3D_ROOT": "/full/path/to/ffed3d"}
    }
  }
}
```

Without `FFELAB_ZIG_ROOT` the `zig_*` tools refuse with "no Zig engine
configured"; without `FFELAB_FFED3D_ROOT`, `seam` refuses with "no ffed3d
checkout configured". The rest works either way. Details:
[docs/STATIC_RESEARCH.md](docs/STATIC_RESEARCH.md).

For the real game set `FFELAB_BACKEND` to `qemu` and `FFELAB_EMU` to a JSON
`EmulatorConfig`, e.g. `{"binary": "<id from exe-check>", "code_base": 0, "gdb_port": 1234}`;
`ffelab emu calibrate` prints yours. Setup on the Mac: [docs/EMULATOR.md](docs/EMULATOR.md).
(`dosbox-x` exists as a backend but needs a DOSBox-X build with a GDB stub.)

## Status

- Working: static map, ledger, adversarial layer, fingerprints, propagation,
  knockouts, exe-check, export, Pydantic models, eval harness with a gate,
  Postgres + pgvector beside SQLite, Agent SDK orchestrator with tracing,
  Temporal campaigns, call-graph structure, asset formats (bitmaps, meshes;
  `docs/ASSET_FORMATS.md`), observation beyond screenshots (watchpoints,
  sound in knockouts, DOS file calls), and the Zig engine bridge
  (`subsystem`, `cut_check`, `seam`, links). Unit-tested against fakes (storage
  tests also on Postgres) plus one real-data test.
- Optional servers (Postgres, Temporal, Phoenix) run in Docker:
  [docs/DOCKER.md](docs/DOCKER.md).
- **Not yet run against a live emulator.** Expect one session of tuning key maps,
  timings and the code base address.
- Not included: the FFE asm (clone ffed3d) and the game itself (bring your own copy).
