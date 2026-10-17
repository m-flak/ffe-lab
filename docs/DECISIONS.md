# Decisions

Why ffelab is built the way it is. Each entry: the decision, the reasoning,
and what would change it. Written so the context survives moving between
chats, Projects and people.

---

### D1. Work from the ffed3d asm, not only a binary
The ffed3d repo (github.com/corpsofengineers/ffed3d) contains John Jordan's
NASM disassembly of FFE (`ffe.asm`, `ffedat.asm`), with ~320 functions already
named by earlier hackers. It is the latest public FFE source; AndyJ's later
FFED3D-AJ source was never published (he died in July 2022).
*Would change if:* AndyJ's source turns up (ask FrontierAstro, Ittiz, the
Frontier forums, Elite-Games.ru).

### D2. Original source files survive as contiguous runs of functions
Braben/Sawyer's asm was split across many files and linked in order, so link
order mirrors file order. Evidence: FUNC numbers follow original addresses with
only 5 breaks; 13 module event tables each anchor a contiguous, non-overlapping
run of handlers, in the same order as the tables in the data segment.
*Consequence:* segmenting the address-ordered list recovers files (AUC 0.81).

### D3. Beliefs, bets and calibration instead of "the AI decides"
LLMs sound equally sure whether right or wrong. Every claim is a bet with a
probability; only experiments settle bets; everyone (human, AI, red team) is
Brier-scored and weighted by their record, per subsystem.
*The human's calls* start with the highest reliability but are scrutinised most
(red team, blind opinions, contradictions), at the human's request.

### D4. Cold-start strategy: observe, contrast, then intervene
Early halts land in unnamed code with no hints. Order of attack:
fingerprints across tagged scenarios → one-tag contrasts → behaviour groups →
propagation from named callees → knockouts (disable and see what vanishes) →
observation with an open label path. Knockouts are the only step that can
answer "what is this?" with no names around.

### D5. Open, hierarchical labels
Beliefs stay over 13 fixed top-level subsystems (well-behaved maths); detail
lives in paths such as `render/planet/atmosphere/clouds`, created on demand.

### D6. Which binary to experiment on
- The asm's `Pos` values are virtual addresses in the `FIRSTENC.EXE` John Jordan
  disassembled (code at file offset 0xE00, loaded at 0x10000).
- **Original DOS game (DOSBox/QEMU, runs on the Mac):** every asm function runs
  as written, including the software renderer. Needs `exe-check` to confirm the
  build or produce an address map.
- **Built FFED3D (Windows VM):** exact symbols from the linker map, compile-time
  instrumentation, source-edit knockouts. But its Direct3D layer bypasses the
  original renderer (knockouts of render code can show nothing), and the public
  repo state has render bugs.
- **Decision:** split by subsystem: original game for rendering/platform code;
  built FFED3D optionally for game logic. Evidence is keyed by FUNC number, so
  both feed the same ledger.

### D7. Names go back into the asm, not into Ghidra
With the source available, Ghidra is only needed for its decompiler or for
binaries without source (FFED3D-AJ). `ffelab export` adds alias labels
(`_FUNC_000688_Name:`) with evidence comments instead of renaming, so every
reference, including FFED3D's C++, keeps working and the source still builds.

### D8. SQLite now, Postgres when agents write concurrently
The dataset is small (1,600 functions, ~6,400 calls); SQLite is fast and keeps
everything in one file. Spark was considered and rejected: built for
multi-machine data, it would only add overhead here. Postgres (+ pgvector)
comes in with concurrent agents and embeddings; SQLite stays as lite mode.

### D9. The AI-engineering stack is chosen for skills *and* fit
Next steps (see `PLAN.md`): Pydantic + evals, Claude Agent SDK + tracing,
Postgres, Temporal/Prefect for durable emulator campaigns. Each solves a real
ffelab problem and is a skill AI-engineering roles ask for.

### D10. Platform: Mac first
Analysis, agents and the original game (DOSBox/QEMU) run natively on macOS.
A Windows VM is only needed for FFED3D-specific work.

---

## Open questions

- Is the downloadable `FIRSTENC.EXE` the build John Jordan disassembled?
  (`ffelab exe-check` answers this.)
- QEMU + FreeDOS vs DOSBox-X for the main emulator: decide at bring-up.
- Temporal vs Prefect: decide when starting step 4 of the plan.
