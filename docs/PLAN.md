# Plan

Status as of v0.5: the AI-engineering stack (items 1–4 below), the call-graph
structure and the asset-format decoding are built. The next real step is
emulator bring-up. Ordered by when
things should happen.

---

## Done

| Version | What |
|---|---|
| v0.1 | asm parser, module event tables, file-boundary segmenter (AUC 0.81), evidence ledger, halt scoring, emulator harness interface, GDB client, MCP server |
| v0.2 | Adversarial layer: experiment-linked bet resolution, experiment-only claim settlement, contradictions, red team, blind second opinions, calibration audit |
| v0.3 | Cold start: fingerprints, one-tag contrasts, behaviour groups, callee propagation, knockouts with noise-masked diffs, open label paths |
| v0.4 | `exe-check` + content-based address mapping between builds; `export` of confirmed names into the asm as alias labels |
| v0.5 | Pydantic models for every record (`models.py`); eval harness (`ffelab eval`) for the blind labeller and red team; `ffelab eval gate` against `evals/baselines/` for prompt changes (run locally; no CI) |
| v0.5 | Agent SDK orchestrator (`ffelab agents`) with budgets, a question queue and OpenTelemetry tracing (Phoenix) |
| v0.5 | Postgres + pgvector beside SQLite (`FFELAB_DB`, `ffelab db copy`), strict run keys, `ffelab embed` / `similar` |
| v0.5 | Durable experiment campaigns on Temporal (`python -m ffelab.workflows`) |
| v0.5 | Call-graph structure (`ffelab graph`): spectral basis, call layers, label quotient |
| v0.5 | Docker Compose for the optional servers (`docs/DOCKER.md`); day-to-day guide (`docs/USAGE.md`) |
| v0.5 | Asset formats: bitmap table and mesh display lists decoded, `tools/meshwalk.py` dumps OBJs (`docs/ASSET_FORMATS.md`); unverified against the running game |

---

## The AI-engineering stack (built in v0.5; what's left is listed per item)

Chosen deliberately: each is a skill AI-engineering roles ask for, and each
solves a real problem in ffelab. Keep SQLite as a zero-setup "lite mode".

### 1. Pydantic models + evals
- Pydantic models for every core record: `HaltSnapshot`, bets, claims,
  observations, experiments, tool inputs/outputs. Validated LLM outputs, no
  free-text parsing.
- Eval harness for the agents: score the blind labeller and red team against
  labels confirmed by experiments (held out). Metrics: accuracy per subsystem,
  Brier/calibration, cost and tokens per correct label. Compare models and
  prompts. Candidates: Inspect AI, promptfoo, or a small custom harness.
- ~~Run evals in CI~~ Dropped: evals run locally, and `ffelab eval gate` fails a
  prompt change that makes agents worse.
- **Status (v0.5): built.** Custom harness chosen (D12). Remaining: once the
  emulator settles labels, `ffelab eval export` real cases, run the agents,
  replace the provisional baselines with `ffelab eval baseline`, and gate on
  the `test` split. Until then the gate runs on synthetic smoke cases.

### 2. Claude Agent SDK orchestrator + tracing
- Own the agent loop as a program instead of only through Claude Code:
  orchestrator plus red-team and blind-labeller workers, token/time budgets,
  stop conditions, a batched question queue for the human.
- Tracing for every agent step, tool call, token count and cost
  (OpenTelemetry + Langfuse or Arize Phoenix).
- **Status: built** (`ffelab agents run`, `docs/ORCHESTRATOR.md`, D14, D15). Next: a first
  live run on the emulator; give the red team `run_halt`/knockouts as orchestrator
  actions; wrap `run_round` as a durable activity (step 4).

### 3. Postgres (+ pgvector)
- Replace SQLite when several agents write concurrently. Same schema; keep a
  SQLite path for single-user/lite mode.
- pgvector for embeddings of normalised asm and fingerprints, giving the
  labeller "find functions like this one" retrieval.
- **Status: built** (D13). `FFELAB_DB=postgresql:///ffelab` switches every
  command and the MCP server; `ffelab db copy` moves an existing `ffe.db` over;
  `ffelab embed` / `ffelab similar` and the `similar_functions` tool do the
  retrieval (pgvector HNSW index on Postgres, NumPy scan on SQLite). Concurrent
  belief updates are locked per function; experiments take an optional unique
  `run_key` for item 4. The blind labeller and red team have `similar_functions`
  in their agent files (added with D16, at Matthew's request, before measuring).
  An `ffe.db` made elsewhere imports with the same `ffelab db copy` (GETTING_STARTED 4.4);
  copying into a non-empty store, i.e. merging two ledgers, isn't supported.
  Remaining: once experiments settle enough labels, `ffelab eval compare` the
  agents with and without it, and drop it if it doesn't help.
- **Call-graph structure: built** (D16, `docs/GRAPH.md`). `ffelab graph` adds a
  spectral basis (`callgraph-spectral-v1`, searchable like the other embeddings),
  call layers, and two static evidence kinds (neighbour vote, quotient check).
  Remaining: once experiments settle labels, check the votes' calibration; run
  the decisive halt in the text-engine cycle. The authorship test against
  Frontier: Elite II is done (`docs/REFERENCES.md`): both halves match FE2 at
  about 10%, so the split looks like subsystem, not author.

### 4. Durable experiment campaigns (Temporal, see D11)
- Long experiment runs as durable workflows: an emulator crash or hang retries
  from the last save state instead of losing the campaign. Activities: load
  state, logging pass, halt, knockout; each idempotent and recorded.
- Temporal chosen (D11).
- **Status: built** (`src/ffelab/workflows/`, `python -m ffelab.workflows`).
  Each step is one transaction keyed by a strict run key (D13's
  `experiments.run_key`), so a retry or a stuck attempt that wakes late can't
  count evidence twice, and a step that dies mid-write leaves nothing. Harness
  failures retry on a fresh backend, hangs time out on missing heartbeats, bugs
  and unknown save states fail once; steer with pause/resume/stop/add_steps.
  Tested on the fake backend against a real Temporal dev server, on SQLite and
  Postgres.
- Remaining: restart QEMU, not just reconnect, when a retry follows a hang;
  human-question signals wired to the orchestrator's question queue (item 2);
  first campaign on the real emulator.

---

## Static-first phase (now, until the emulator runs)

- An outside researcher (Muse) works from code and data alone through
  `ffelab research` (view / refs / grep to read; claim / import to write) under
  one source name, `ai:muse`. It makes claims only; experiments settle them
  later. Protocol: `docs/STATIC_RESEARCH.md`.
- Muse's first `ffe.db` imports with `ffelab db copy`, then
  `ffelab research retag ai:lut-analysis ai:muse` (GETTING_STARTED 4.4).
- Later: a `static-researcher` worker under `ffelab agents run`, with the same
  brief and the same claims-only tools.

---

## Emulator bring-up (in parallel with the above)

Which binary, and why: see `DECISIONS.md` (D6). Short version: original DOS
game in DOSBox/QEMU on the Mac for rendering/platform code; built FFED3D in a
Windows VM optionally for game logic.

1. Download the game, run `ffelab exe-check FIRSTENC.EXE`. Same build: identity
   map. Different: aligned map, check `suspected_changed`.
2. QEMU + FreeDOS (DOSBox-X only if QEMU can't run FFE: it needs a debugger
   port first). Step by step: `docs/EMULATOR.md`.
3. Calibrate `code_base` with `ffelab emu calibrate` (it checks for
   `55 8B EC` / `55 89 E5` at `code_base + Pos` of known framed functions).
4. Build and tag the save-state library with `ffelab scenario save`
   (starter set in `docs/EMULATOR.md` step 9).
5. First real logging pass, knockout, and red-team round; fix what breaks.

Optional: a **built-FFED3D backend** (linker map for exact symbols, compile-time
hit counters instead of breakpoints, knockouts as source edits). Windows VM only.

---

## Later

**Assets**
- Check the mesh dumps against what the game renders; decode the remaining
  opcodes (`0x05` complex polygons, sub-objects, set color, text).
- Recover the runtime palette; settle `FUNC_001805`/`FUNC_001807` (open
  `render/texture/palette` claims from the LUT analysis) with a knockout once
  the emulator runs.

**Static map**
- More boundary features: DATA ownership, string-literal locality, local-vs-exported call ratio.
- Map DATA labels to segments (data order should mirror code order).
- Clean the family list (Gui/Keyb/Mouse/Timer, CdrFile/SysMenu may each be one file).
- Author/style features: Sawyer vs translated-from-68k vs other.

**Experiments**
- Call-site knockouts (needs instruction addresses in the parser), return-value
  knockouts, data knockouts, audio diffs.
- Memory-map bootstrap: confirmed addresses for player position, docked flag,
  current system, so experiments read state directly.
- Use behaviour groups as soft segment evidence.

**Adversarial**
- Red team attacks segment boundaries and inherited names, not just claims.
- Hold-out discipline: periodically re-test 10% of confident labels blind.
- Tune `halt.py` RULES from resolved bets instead of hand-set factors.

**More overkill (if wanted)**
- MLflow for segmenter/labeller model versions and metrics.
- DuckDB/Polars + Parquet for experiment-history analytics.
- Docker image for QEMU + FreeDOS + ffelab.
- PyTorch Geometric: function labelling as node classification on the call graph.

**FFED3D-AJ (Windows VM)**
- Match AndyJ's binaries against rev16/rev60 builds with symbols; study his additions.

---

## References

- **Frontier: Elite II via glFrontier (D17, `docs/REFERENCES.md`): built.**
  `ffelab ref import` stores about 160 matches. Next, if wanted: an FE2-name to
  subsystem map so matches can become `static_ref` evidence; FE2's notes (ship
  type ids, `FmtStr` codes, module slots 24/28) as hypotheses for halts.

---

## Parked side findings

- **Planet render bug lead (FFED3D C port).** `win/planet.cpp` `C_FUNC_001869` is a
  machine-decompiled port of asm `FUNC_001869` (Pos 0x6881c). The asm does
  `mov eax,0xffffb000; sar eax,cl`; the C does `0xffffb000 >> ecx` on an unsigned
  constant (logical shift). At subdivision depth 1 this flips the sign of the
  visibility test whenever `distToVertex` is odd, in all three per-vertex checks.
  Portable fix: `eax = distToVertex * -(0x5000 >> ecx);`. Unverified on screen.
  Next: scan other `C_FUNC_*` ports for the same bug class (shifts of unsigned
  hex constants, signed/unsigned mismatches); verify with a Unicorn differential
  test (original asm vs C port on the same inputs).
- **Long-term host idea.** Zig host + Unicorn running the original asm,
  Vulkan/Metal presentation, incremental replacement of understood functions
  (OpenRCT2-style). Real blocker is 32-bit x86, not the renderer. Not started.
