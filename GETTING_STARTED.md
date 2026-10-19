# Getting started with ffelab

A beginner's guide to the workflow: what the pieces are, how they fit, and
what you do day to day. No prior AI-engineering experience assumed.

---

## 1. What this project is, in one paragraph

You have the FFE game code as x86 assembly (from the ffed3d repo), but most of
its ~1,600 functions have no meaningful names. ffelab helps you figure out what
each one does. It does three things: it **maps** the code into the original
source files (statically, from the asm), it lets an **AI agent run experiments**
on the real game in an emulator (breakpoints, knockouts), and it **keeps score**
of every guess — yours, the AI's, and the red team's — so the conclusions rest
on evidence rather than on whoever sounded most confident.

---

## 2. The mental model

Think of it as a lab notebook with a referee.

```
          you + AI agents
     (guesses, claims, questions)
                 │
                 ▼
   ┌──────────────────────────────┐        ┌──────────────────────┐
   │   ffe.db  (the notebook)     │◀──────▶│  the game, running   │
   │   beliefs · bets · evidence  │  runs  │  in an emulator      │
   │   experiments · labels       │        │  (breakpoints,       │
   └──────────────────────────────┘        │   knockouts)         │
                 │                         └──────────────────────┘
                 ▼
        names you can trust
```

- **The notebook** (`ffe.db`, a SQLite file, or the same tables in Postgres)
  holds everything anyone knows.
  Nothing important lives only in a chat. That's what lets you stop, come back
  tomorrow, and continue.
- **The agents** (Claude, via Claude Code) read the notebook, propose what to
  test, drive the game, and write results back.
- **You** are the senior reverser. Your calls carry the most weight, and get
  the most scrutiny.
- **The referee** is the set of rules in the code: bets are only settled by real
  experiments, contradictions get flagged, everyone's accuracy is tracked.

---

## 3. Glossary (read once, refer back)

| Term | Meaning here |
|---|---|
| **Function** | One `FUNC_000123` in `ffe.asm`. The numbers follow the original memory layout. |
| **Segment** | A run of adjacent functions the model thinks came from one original source file. |
| **Event table** | A 12-slot table in `ffedat.asm` listing one module's handlers (reset, escape, action…). These anchor the segments. |
| **Label** | What a function is for. Top level is one of 13 subsystems: `render`, `physics`, `ai`, `ui`, `mission`, `economy`, `sysgen`, `io`, `audio`, `math`, `text`, `platform`, `unknown`. |
| **Label path** | A more specific label under a subsystem, e.g. `render/planet/atmosphere/clouds`. New paths are created whenever needed. |
| **Belief** | For each function, a probability for each top-level label. Starts even; evidence moves it. |
| **Evidence** | Anything that moves a belief: an experiment result, a call, a propagated hint. |
| **Call / claim** | Someone (you, an AI) says "FUNC X is label Y, Z% sure". Recorded as a bet. |
| **Bet** | A prediction with a probability. Settled later by an experiment, then scored. |
| **Experiment** | A real run of the game: a logging pass, a halt, or a knockout. Only experiments settle bets. |
| **Logging pass** | Load a save state, count how often each of many functions runs. Cheap, broad. |
| **Halt** | Freeze the game at one function and capture the call stack, registers and arguments. Detailed, narrow. |
| **Knockout** | Make a function return immediately, replay, and compare screenshots. Shows what it does by what disappears. |
| **Fingerprint** | A function's activity in each scenario (silent / rare / regular / per-frame). |
| **Behaviour group** | Functions with matching fingerprints: a subsystem found before it has a name. |
| **Propagation** | Unknown functions picking up weak hints from the labels of functions they call. |
| **Contradiction** | Experiments strongly disagree with a confident claim. Must be reviewed before naming. |
| **Red team** | An agent whose job is to prove claims wrong, scored on its own accuracy. |
| **Blind labeller** | An agent that labels functions from code alone, without seeing anyone else's opinion. |
| **Brier score** | How wrong a probability was: (said − happened)². 0 is perfect; saying 50% on everything scores 0.25. |
| **Calibration** | Whether your 90%-sure claims are right about 90% of the time. |
| **Reliability** | A 0–1 score per person/agent (and per subsystem) derived from their Brier scores. Scales how much their calls count. |
| **AUC** | For the segment map: pick a real file boundary and a non-boundary at random; AUC is how often the model scores the real one higher. 0.5 = guessing, 1.0 = perfect. Currently 0.81. |
| **MCP** | Model Context Protocol: the standard way to give an AI agent tools. ffelab runs an MCP server exposing ~35 tools to Claude. |
| **Scenario** | A named emulator save state (e.g. `orbit_atmo`) with tags describing it. |

---

## 4. One-time setup

### 4.1 Get the code

```bash
mkdir -p ~/re && cd ~/re
git clone https://github.com/corpsofengineers/ffed3d     # the FFE asm source
git clone https://github.com/m-flak/ffe-lab
cd ffe-lab
python3 -m venv .venv
source .venv/bin/activate          # do this every time you open a new terminal
pip install -e ".[agent,evals,dev]"
```

The other extras are for optional pieces, added when you reach them: `pg`
(Postgres, 4.4), `orchestrator` (unattended agent runs), `durable` (Temporal
campaigns). `docs/USAGE.md` says when each one is worth it.

### 4.2 Build the database and module map

```bash
ffelab build --src ~/re/ffed3d
```

This creates:
- `ffe.db` — the notebook
- `docs/MODULE_MAP.md` — the recovered source-file map (open it, it's readable)
- `data/module_map.json` — the same thing as data

Check everything works:

```bash
python -m pytest                              # should pass; some tests skip until their extra or server is installed
FFED3D_DIR=~/re/ffed3d python -m pytest       # also runs the real-data test
```

### 4.3 Connect Claude Code

Claude Code is Claude running in your terminal, inside your project folder. It
can read files, run commands and use MCP tools.

Create `.mcp.json` in the project root:

```json
{
  "mcpServers": {
    "ffelab": {
      "command": "/Users/YOU/re/ffe-lab/.venv/bin/python",
      "args": ["-m", "ffelab.mcp_server"],
      "env": {
        "FFELAB_DB": "/Users/YOU/re/ffe-lab/ffe.db",
        "FFELAB_BACKEND": "fake"
      }
    }
  }
}
```

Use full paths (replace `YOU`). `FFELAB_BACKEND=fake` means "no real game yet":
all the notebook tools work, game-running tools use a pretend game.

Start Claude Code in the folder (`claude`) and ask:

> "List the ffelab tools you have."

If it lists tools like `belief`, `blind_view`, `run_knockout`, you're connected.
The two agents in `.claude/agents/` (`red-team`, `blind-labeller`) are picked up
automatically; you invoke them by asking for them by name.

### 4.4 Optional: Postgres and the vector search

`ffe.db` (SQLite) is fine for you plus one agent. Move to Postgres when several
agents write the ledger at the same time, or when you want the "find
functions like this one" search to run in the database (pgvector, the
vector-search extension). Nothing else changes: same tables, same commands,
same tools.

**Get a server.** Either one works:

- *Docker* (the default; nothing to install on the Mac): `docs/DOCKER.md`.
  In short: `cp .env.example .env`, `docker compose --profile pg up -d --wait`,
  then `set -a; source .env; set +a`. That sets
  `FFELAB_DB=postgresql://ffelab:ffelab@localhost:5432/ffelab`.
- *Homebrew*:

  ```bash
  brew install postgresql@17 pgvector
  export PATH="$(brew --prefix postgresql@17)/bin:$PATH"   # versioned formula, not on PATH by default
  brew services start postgresql@17
  createdb ffelab
  export FFELAB_DB=postgresql:///ffelab
  ```

  (Postgres.app, which bundles pgvector, works too.)

Then install the Python driver: `pip install -e ".[pg]"`.

**Import an existing `ffe.db`.** This works for any `ffe.db` written by
ffelab, including one another tool or session made (for example the one from
the Muse asset and texture-loading research). Copy it next to the repo first;
the commands below call it `ffe.db`.

```bash
ffelab db copy --from ffe.db --to "$FFELAB_DB"   # every table, ids kept
ffelab embed                                     # rebuild the vectors (the copy skips them)
ffelab graph build                               # refresh call layers + call-graph basis
ffelab research retag ai:lut-analysis ai:muse    # Muse's file only: one source name for Muse
```

The `retag` line is for the Muse file: it moves Muse's claims from the topic
name it used (`ai:lut-analysis`) to its one source name, `ai:muse`, so all of
Muse's work is scored as one record (docs/STATIC_RESEARCH.md says why).

Check it landed:

```bash
ffelab uncertain -n 5                    # the ledger answers from Postgres
ffelab similar 1805                      # code-similarity search
ffelab similar 1805 --model callgraph-spectral-v1   # call-graph neighbours
```

What to know about the copy:

- **The destination must be empty.** It refuses otherwise ("destination is not
  empty"), so it never mixes two ledgers. To start over: `dropdb ffelab && createdb ffelab`
  (Docker: `docker compose exec postgres dropdb -U ffelab --force ffelab`, then
  `docker compose exec postgres createdb -U ffelab ffelab`).
- **It moves one ledger; it doesn't merge two.** If you have two `ffe.db`
  files with different work in them, pick the one to keep, or re-record the
  other's calls through Claude Code ("record a call: ...") so they land as new
  claims.
- **Nothing gets promoted.** Claims come across as open claims with their
  original source and confidence (Muse's are under `ai:lut-analysis`, `ai:muse`
  after the retag, and `ai:blind`). Only experiments settle them, same as before.
- **No need to re-run `ffelab build`.** The static map (functions, calls,
  segments) is copied too. Static graph votes come across as well; re-run
  `ffelab graph vote` only after labels change.
- The orchestrator's queued questions (`ffelab agents questions`) are not
  copied. Answer or dismiss them before moving.

**Point everything at it.** Every `ffelab` command, the Temporal worker and the
MCP server read `FFELAB_DB` (`--db` overrides it for one command). Put the same
URL in `.mcp.json` as `"FFELAB_DB"` so Claude Code's tools use it too. To run
the test suite on Postgres: `createdb ffelab_test`, then
`FFELAB_TEST_PG=postgresql:///ffelab_test python -m pytest` (the Docker
container already has `ffelab_test`).

**Re-run `ffelab embed`** after new functions or new logging passes, so the
fingerprint vectors catch up.

More graph commands, once the ledger is in place:

```bash
ffelab graph check                       # how well the graph votes predict known labels
ffelab graph vote                        # write them as static evidence (never settles anything)
ffelab graph outliers                    # calls between labels nothing else supports
```

Terms: an *embedding* is a list of numbers describing a function, built so
similar functions get similar lists; *similarity* is the cosine between two
of them (1 = same, 0 = unrelated). ffelab's embeddings come from the code's
instruction patterns with names stripped, and from which scenarios a function
fires in. They point you at candidates; only experiments settle anything.
The call-graph basis places functions by who calls whom; the *quotient* is the
small label-to-label call graph you get by collapsing each labelled function to
its label, which rules out most labels for a function from its labelled
neighbours. Findings and numbers: `docs/GRAPH.md`. Without pgvector on the
server, ffelab stores vectors as bytes and searches them in Python, which is
slower but gives the same answers.

---

## 5. The workflow in three stages

### Stage 1 — Static work (you can do this today)

No emulator needed. You read code and make calls; the AI assists.

**Things to say to Claude Code:**

| You want to… | Say something like… |
|---|---|
| Orient yourself | "Summarise docs/MODULE_MAP.md. Which segments are biggest and least understood?" |
| Look at a function | "Show me the blind view of FUNC 688 and explain what the code does." |
| Record what you know | "Record a call: 688 is ai/traffic/spawning, 85% confidence, source human." |
| Spread knowledge | "Run label propagation and tell me which functions moved most." |
| Find look-alikes | "Use similar_functions on 1805 and tell me which neighbours already have labels." |
| Use the call graph | "Run graph_votes, then list unnamed functions whose belief moved most and why." |
| Find suspect labels | "Show quotient_outliers and have the red team look at the confident claims there." |
| Get a second opinion | "Use the blind-labeller agent on the first ten functions of segment 12." |
| Find disagreements | "Reveal the blind labels for those and list disagreements with my calls." |
| See what's most uncertain | "Which functions are most uncertain right now?" |

**An outside researcher** (Muse today) works the same phase from the command
line: it reads with `ffelab research view / refs / grep` and writes only
claims with `ffelab research claim`. Hand it `docs/STATIC_RESEARCH.md` as its brief.

**Tips:**
- **Be honest with confidence.** 60% is a perfectly good call. Your accuracy is
  measured per confidence level; saying 95% when you mean 70% will cost you
  reliability later.
- **Use paths when you know more.** `render/planet/terrain` beats `render`.
- **"unknown" is a valid answer.** A confident wrong label is the most damaging
  thing in the notebook.

### Stage 2 — Emulator setup (one focused session)

This is the part that hasn't been tested against a real game yet. Expect fiddling.

0. **Check your game executable first.** The asm's addresses describe one
   specific `FIRSTENC.EXE` build. Find out whether yours is it:

   ```bash
   ffelab exe-check /path/to/FIRSTENC.EXE
   ```

   - `same_build`: addresses line up directly.
   - `related_build`: a patched/different version; ffelab aligns the two by
     content and stores an address map. `suspected_changed` lists functions that
     differ between builds (often the patch fixes).
   - `different_layout`: something else entirely; ask Claude for help.

   Note the `binary_id` it prints; you'll put it in `FFELAB_EMU` in step 3.
   Why the original game rather than your built FFED3D: see `docs/DECISIONS.md` (D6).

1. **Run your copy of FFE in QEMU + FreeDOS on the Mac.** QEMU is the only
   emulator ffelab can drive end to end today: stock DOSBox-X has no debugger
   port. Step-by-step Mac setup (QEMU + FreeDOS, code_base calibration, tagged save states): see [docs/EMULATOR.md](docs/EMULATOR.md).
2. **Calibrate the address offset** (`code_base`): with the game running,
   `ffelab emu calibrate --exe /path/to/FIRSTENC.EXE --binary <binary_id>`.
   It finds the code in memory, checks that functions start with
   `push ebp; mov ebp,esp` at `code_base + Pos`, and prints your `FFELAB_EMU` line.
3. **Switch the backend** in `.mcp.json`: `"FFELAB_BACKEND": "qemu"`, plus
   `"FFELAB_EMU"` with the line calibrate printed, e.g.
   `"{\"binary\": \"<binary_id>\", \"code_base\": 1048576, \"gdb_port\": 1234, \"monitor_port\": 4444}"`.
4. **Build the save-state library**: about 10–15 situations. Play to each one,
   then save and tag it in one go:

   ```bash
   ffelab scenario save orbit_atmo --tags near_planet,atmosphere,altitude_high
   ffelab scenario list                      # tags, and whether QEMU has the snapshot
   ```

   (`ffelab scenario tag NAME --tags ...` retags without saving.)

   **Plan pairs that differ by one tag** (atmosphere vs. airless, high vs. low
   altitude, day vs. night side). Those pairs are controlled experiments and are
   the most valuable thing you'll make.

Suggested starter set:

| scenario | tags |
|---|---|
| `main_menu` | menu |
| `docked` | docked |
| `launching` | docked, launching |
| `deep_space` | space |
| `orbit_atmo` | near_planet, atmosphere, altitude_high |
| `orbit_airless` | near_planet, altitude_high |
| `low_atmo` | near_planet, atmosphere, altitude_low |
| `surface` | near_planet, atmosphere, planet_surface |
| `combat` | space, combat |
| `hyperspace` | hyperspace |

### Stage 3 — The daily loop

Start a session with a goal and a time box:

> "Work on segment 10 for an hour. Fingerprint its functions across all
> scenarios. Knock out anything whose fingerprint is ambiguous and record what
> you see in the diffs. Run propagation. Then have the red-team agent attack the
> three most important claims. Ask me when you're stuck."

**The agent** drives the game, records experiments, and writes results.

**You:**
1. **Answer questions** — the quick ones ("does that sound play on docking?") are
   the best use of your time.
2. **Make calls** when results remind you of something.
3. **Review contradictions** at the end:
   > "Show open contradictions."

   For each one, decide: was the experiment flawed (`explained`) or were you
   wrong (`claim_withdrawn`)? A function can't be named while it has an open
   contradiction.
4. **Skim `knockouts/`** — before/after/diff images. The fastest, most convincing
   evidence in the system.

**Weekly:**
> "Show my calibration."

You'll see your Brier score, whether you're over- or under-confident, a curve of
"said vs. was right", and your reliability per subsystem. Use it to adjust how
confident you sound.

**Whenever names firm up — export them into the asm:**

```bash
ffelab export --src ~/re/ffed3d --out export          # committed names only
ffelab export --src ~/re/ffed3d --out export --auto   # + experiment-backed label paths
```

This writes `export/ffe.asm` (your source is never touched), `export/ffe.asm.diff`
to review, and `export/export_report.json`. Names are added as alias labels
(`_FUNC_000688_SpawnTraders:`) with an evidence comment, so existing references
and the FFED3D build keep working. Functions with open contradictions are skipped.

---

## 6. Handling a function nobody can identify

The common case early on. The sequence:

1. **Logging passes across scenarios** → a fingerprint ("per-frame only when near
   an atmospheric planet").
2. **One-tag contrasts** → which condition flips it on/off ("responds to
   atmosphere").
3. **Behaviour group** → what else behaves the same.
4. **Propagation** → hints from what it calls ("calls translucent-texture code").
5. **Knockout** → turn it off, see what disappears.
6. **Observation** → name what vanished, with a path:
   `render/planet/atmosphere/clouds`.

Ask for it in one go:

> "FUNC 10 is a mystery. Fingerprint it, find which tag it responds to, knock it
> out in the scenario where it's most active, and show me the diff."

### Long runs: durable campaigns (optional)

For runs of hundreds of experiments, let Temporal drive them so an emulator
crash or hang retries from the save state instead of losing the run (D11):

```bash
pip install -e '.[durable]'
brew install temporal                                       # or skip these two lines and use
temporal server start-dev --db-filename campaigns.db        # Docker's `temporal` profile (docs/DOCKER.md); UI: http://localhost:8233
python -m ffelab.workflows worker --db ffe.db --backend qemu --emu '{"code_base": 0}'   # your calibrated code_base (Stage 2)
python -m ffelab.workflows start plan.json                  # in another terminal
python -m ffelab.workflows status NAME                      # or pause / resume / stop
```

`plan.json` lists steps, or lets the campaign pick its own targets:

```json
{"name": "orbit-sweep",
 "steps": [{"kind": "knockout", "scenario": "orbit_atmo", "funcs": [10]}],
 "auto":  [{"kind": "logging_pass", "scenario": "orbit_atmo", "count": 40,
            "context": ["near_planet", "atmosphere"]}]}
```

Try it first with `--backend fake --emu '{"truth": {"orbit_atmo": {"10": 1.0}}}'`.

---

## 7. What's built, and what isn't yet

Built, each with its own page:

- **Unattended agent runs** (`ffelab agents run`): the blind labeller and red
  team as workers under a dollar/time budget, every step traced, and a batched
  question queue for you (`ffelab agents questions`). `docs/ORCHESTRATOR.md`.
  Until the emulator works, run it static-only: `ffelab agents run --attacks 0`.
  **Warning:** the red team's attacks run real logging passes on `--backend`,
  which defaults to `fake`. Against your real ledger, attacks on the fake
  backend record made-up experiments and resolve bets with them. Use
  `--attacks 0`, or point `--db` at a scratch copy, until `--backend qemu` works.
- **Evals** (`ffelab eval`): score the agents by a number before keeping a
  prompt or model change. `docs/EVALS.md`.
- **Postgres + vector search** (4.4), **Temporal campaigns** ("Long runs"
  above), **call-graph structure** (`docs/GRAPH.md`), and **Docker** for the
  servers (`docs/DOCKER.md`).
- **Game asset formats**: bitmaps and the 465-model mesh table, with
  `python3 tools/meshwalk.py all --src ~/re/ffed3d` dumping meshes as OBJ
  files. `docs/ASSET_FORMATS.md`. Not yet checked against the running game.

Not built yet:

- **The real emulator backend is untested.** Stage 2 is where that gets fixed,
  and almost everything above needs experiments to say anything real.
- **A built-FFED3D backend** (linker-map symbols, compile-time counters): optional.
- **Call-site, return-value and data knockouts**: only whole-function knockouts exist.
- **Merging two ledgers**: `ffelab db copy` moves one whole store into an empty one.

`docs/USAGE.md` puts all of it in order; `docs/PLAN.md` has the roadmap.

---

## 8. The claude.ai Project

The repo is on GitHub (github.com/m-flak/ffe-lab) and the claude.ai Project
"FFE LAB" is set up with it. Three layers of context, from most to least reliable:

1. **The repo files**: the durable record. `docs/DECISIONS.md` holds the
   reasoning behind the design; `docs/PLAN.md` holds what's next.
2. **The ledger** (`ffe.db` or Postgres): every claim, experiment and score.
   Keep `ffe.db` out of git (it's in `.gitignore`); `ffelab build` rebuilds the
   static part, and the ledger itself moves with `ffelab db copy`.
3. **Chat**: scratch. Anything worth keeping goes into the repo or the ledger.

Use the Project for planning and discussion; use Claude Code in the repo for
the hands-on work (it's the one with the tools and the game).

---

## 9. Troubleshooting

| Problem | Try |
|---|---|
| `ffelab: command not found` | `source .venv/bin/activate` first |
| Claude Code shows no ffelab tools | Check `.mcp.json` uses full paths; restart Claude Code |
| `ModuleNotFoundError: mcp` | `pip install -e ".[agent]"` |
| "Postgres support needs psycopg" | `pip install -e ".[pg]"` |
| `connection refused` on a `postgresql://` URL | `brew services start postgresql@17` |
| `db copy` says "destination is not empty" | It never merges; empty the database first (4.4, "start over") |
| `similar` says "has no ... embedding" | Run `ffelab embed` (again after new functions or logging passes); for `callgraph-spectral-v1`, `ffelab graph build` |
| Tests fail on `TestRealData` | Set `FFED3D_DIR` to the ffed3d folder, or ignore (it's optional) |
| "bets can only be resolved by a recorded experiment" | Working as intended: run the experiment first |
| "has an open contradiction; resolve it before naming" | `show open contradictions`, then close it |
| Knockouts always say "frozen" | Use an animated scene; static scenes look frozen to the heuristic |
| `exe-check` says `different_layout` | Probably not the DOS FIRSTENC.EXE; check the file and its size (~2 MB+) |
| Export added nothing | Names need belief ≥ `--min-prob` (0.9) and no open contradiction; try `--auto` |
