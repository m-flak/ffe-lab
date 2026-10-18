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

### D12. Evals: a small custom harness, gold labels from experiments only
Inspect AI and promptfoo were the candidates. promptfoo is Node and YAML-first;
Inspect is the better long-term fit but would wrap a ~400-line need (load
cases, call one agent, score, gate) in a framework before there is any real
data to score. The custom harness reads the ledger directly and its parts map
one to one onto Inspect's (dataset, solver, scorer), so a move later is mechanical.
Gold labels obey the ledger rules: a case exists only when experiment-only
evidence is decisive (the `settle_claims` test), never from calls, inherited
names or static derivations. Eval answers are measurements of the agent and are
never written to the ledger. Cases are split dev/test by a stable hash; tune on
dev, only read the test score.
*Would change if:* we need multi-turn agent evals with tool use and sandboxes
(the red team running real experiments), or a model-graded rubric. That is the
point to move to Inspect, likely alongside the Agent SDK orchestrator (PLAN 2).

### D13. Postgres beside SQLite: one schema, plain SQL, a thin adapter (no ORM)
`db.connect()` picks the backend from its argument: a file path is SQLite (lite
mode, still the default), a `postgresql://` URL is Postgres. The schema is
written once, in SQLite's dialect, and rendered for Postgres (identity ids,
8-byte floats, a pgvector column). Queries are SQL both accept (`ON CONFLICT`
upserts, `RETURNING id`); `pg.py` only translates placeholders and timestamps
and makes rows look like `sqlite3.Row`. An ORM (SQLAlchemy) was rejected: it
would mean rewriting every module's queries to gain little, and `models.py`
(D12's Pydantic records) is already the typed layer.
*Concurrent agents:* a function's belief is read, updated and written under a
per-function lock (a Postgres advisory lock; `BEGIN IMMEDIATE` on SQLite), so
two agents can't both update from the same prior and lose one update. A bet
resolves once: the first resolver wins, the rest get `AlreadyResolved`.
*Retries:* `experiments.run_key` is unique and optional; recording the same key
again returns the existing experiment, so a retried campaign step (PLAN 4)
can't count as two experiments. For a step whose timed-out attempt may still
be running, `record_experiment(..., if_exists="raise")` inside
`with ledger.atomic():` makes the experiment, its key and its evidence one
transaction: the attempt that loses the key raises `RunKeyExists` and writes
nothing, and a crash mid-step leaves no half-recorded experiment.
*Similarity:* embeddings are local, deterministic hashed instruction n-grams and
scenario hit rates, not an embedding API: free, reproducible, no key, and
built from code with names stripped, so results (function numbers and scores
only) are safe for the blind labeller. They are hints for where to look and
never evidence; they don't touch beliefs or bets.
*Would change if:* the eval harness shows retrieval doesn't help labelling (drop
it) or helps but misses (try a code embedding model, measured the same way);
or the adapter's translations grow beyond a handful (then SQLAlchemy Core).

### D16. Call-graph structure as static evidence: spectral basis, layers, quotient
Functions get coordinates from the call graph's own spectral basis (32 smoothest
Laplacian eigenvectors, stored as embedding `callgraph-spectral-v1`), call layers
(height/depth with cycles collapsed) and a label quotient. Learned axes beat
hand-picked ones (fan-in, fan-out, link-order distance): 5-NN family agreement
63% vs 34% (chance 12%), above the code n-gram embedding's 50%. Two static
evidence kinds come out of it: the neighbours' labels and the labels the quotient
allows. Numbers and findings: `docs/GRAPH.md`.
*Why static evidence, not a filter:* it shapes beliefs and experiment choice
like callee propagation does, but never settles a claim or raises a
contradiction (ledger rules, D3). Votes only use labels known without static
evidence, so they can't feed on themselves.
*Why unweighted, undirected:* call-site weights and direction (magnetic
Laplacian) scored no better on the real graph; direction is used non-linearly,
as call layers.
*Blind labeller:* neighbour search returns numbers only and layers are
structure only. The label vote uses others' labels and stays out of its view.
*Would change if:* experiment-settled labels show the votes are miscalibrated
(their source reliability drops; retune or drop the factors), or a learned
model (PLAN "Later": node classification) beats them on the same hold-out.

---

## Open questions

- Is the downloadable `FIRSTENC.EXE` the build John Jordan disassembled?
  (`ffelab exe-check` answers this.)
- QEMU + FreeDOS vs DOSBox-X for the main emulator: decide at bring-up.
- Temporal vs Prefect: decide when starting step 4 of the plan.
