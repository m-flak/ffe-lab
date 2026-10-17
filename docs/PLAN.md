# Plan

Status as of v0.4. Ordered by when things should happen.

---

## Done

| Version | What |
|---|---|
| v0.1 | asm parser, module event tables, file-boundary segmenter (AUC 0.81), evidence ledger, halt scoring, emulator harness interface, GDB client, MCP server |
| v0.2 | Adversarial layer: experiment-linked bet resolution, experiment-only claim settlement, contradictions, red team, blind second opinions, calibration audit |
| v0.3 | Cold start: fingerprints, one-tag contrasts, behaviour groups, callee propagation, knockouts with noise-masked diffs, open label paths |
| v0.4 | `exe-check` + content-based address mapping between builds; `export` of confirmed names into the asm as alias labels |
| v0.5 | Pydantic models for every record (`models.py`); eval harness (`ffelab eval`) for the blind labeller and red team; tests + offline evals in CI, live agent evals gated on `evals/baselines/` (needs `ANTHROPIC_API_KEY`) |

---

## Next: the AI-engineering stack (start once the claude.ai Project exists)

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
- Run evals in CI so a prompt change that makes agents worse fails the build.
- **Status (v0.5): built.** Custom harness chosen (D12). Remaining: once the
  emulator settles labels, `ffelab eval export` real cases, run the agents,
  replace the provisional baselines with `ffelab eval baseline`, and gate on
  the `test` split. Until then CI runs on synthetic smoke cases.

### 2. Claude Agent SDK orchestrator + tracing
- Own the agent loop as a program instead of only through Claude Code:
  orchestrator plus red-team and blind-labeller workers, token/time budgets,
  stop conditions, a batched question queue for the human.
- Tracing for every agent step, tool call, token count and cost
  (OpenTelemetry + Langfuse or Arize Phoenix).

### 3. Postgres (+ pgvector)
- Replace SQLite when several agents write concurrently. Same schema; keep a
  SQLite path for single-user/lite mode.
- pgvector for embeddings of normalised asm and fingerprints, giving the
  labeller "find functions like this one" retrieval.

### 4. Durable experiment campaigns (Temporal or Prefect)
- Long experiment runs as durable workflows: an emulator crash or hang retries
  from the last save state instead of losing the campaign. Activities: load
  state, logging pass, halt, knockout; each idempotent and recorded.
- Decide Temporal vs Prefect when starting: Temporal for strict durability and
  agent-style long workflows, Prefect for a lighter Python-native setup.

---

## Emulator bring-up (in parallel with the above)

Which binary, and why: see `DECISIONS.md` (D6). Short version: original DOS
game in DOSBox/QEMU on the Mac for rendering/platform code; built FFED3D in a
Windows VM optionally for game logic.

1. Download the game, run `ffelab exe-check FIRSTENC.EXE`. Same build: identity
   map. Different: aligned map, check `suspected_changed`.
2. QEMU + FreeDOS (most scriptable) or DOSBox-X (most compatible).
3. Calibrate `code_base`: bytes at `code_base + Pos` of a known framed function
   must be `55 8B EC` or `55 89 E5`.
4. Build and tag the save-state library (starter set in `GETTING_STARTED.md`).
5. First real logging pass, knockout, and red-team round; fix what breaks.

Optional: a **built-FFED3D backend** (linker map for exact symbols, compile-time
hit counters instead of breakpoints, knockouts as source edits). Windows VM only.

---

## Later

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
- Docker image for QEMU + FreeDOS + ffelab; GitHub Actions running tests and evals.
- PyTorch Geometric: function labelling as node classification on the call graph.

**FFED3D-AJ (Windows VM)**
- Match AndyJ's binaries against rev16/rev60 builds with symbols; study his additions.

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
