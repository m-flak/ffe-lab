# Using ffelab, start to finish

The short version of how to use ffelab with the AI-engineering stack (evals,
Postgres, the orchestrator, Temporal campaigns, the call graph). It says what
to run, in what order, and what *you* do at each step. `GETTING_STARTED.md` has
the long explanations and the glossary; this page is the map.

## The one thing to know first

Almost everything new (evals with real answers, the orchestrator's red team,
Temporal campaigns) needs **experiments on the real game**, and experiments need
the emulator. Until the emulator works, ffelab can only do static work and dry
runs against a fake game. So the order is fixed:

```
install → build ffe.db → exe-check FIRSTENC.EXE → emulator bring-up → daily loop
                                                                       (+ optional pieces)
```

## What's required and what's optional

| piece | needed? | use it when |
|---|---|---|
| `ffelab build`, ledger, MCP tools, Claude Code | **required** | always: this is the core |
| `exe-check` + emulator (QEMU or DOSBox-X) | **required** for anything beyond static work | before your first experiment |
| SQLite (`ffe.db`) | default | you plus one agent at a time |
| Postgres + pgvector | optional | several agents writing at once, or you want `ffelab similar` in the database (it also works on SQLite) |
| Orchestrator, `ffelab agents run` | optional | you want agents to work unattended under a dollar/time budget instead of chatting in Claude Code |
| Phoenix tracing | optional | you want to see every agent step, tool call and its cost |
| Temporal campaigns | optional | runs of hundreds of experiments that must survive an emulator crash or hang |
| Evals | optional, but run before keeping a prompt or model change | you edited `.claude/agents/*.md` or want to compare models |

Postgres, Temporal and Phoenix are servers. The default way to get them is
Docker (`docs/DOCKER.md`): one `docker compose` file runs all three, so
you install none of them on the Mac. ffelab, Claude Code and the emulator
stay on the Mac either way.

Terms: an **agent** is a model given tools that it calls in a loop. The
**orchestrator** is ordinary Python that hands agents small tasks and enforces
the budget and ledger rules. A **trace** is the timed record of one run.
An **eval** is a fixed set of questions with known answers, used to score an
agent by a number instead of by feel. **Temporal** is a workflow engine: it
saves a long job step by step so it resumes after a crash.

---

## 1. Install (once, on the Mac)

```bash
mkdir -p ~/re && cd ~/re
git clone https://github.com/corpsofengineers/ffed3d
git clone https://github.com/m-flak/ffe-lab && cd ffe-lab
python3 -m venv .venv && source .venv/bin/activate      # activate in every new terminal
pip install -e ".[agent,evals,dev]"                       # the core, plus evals
python -m pytest                                          # should pass
```

Add extras only when you reach the optional piece: `.[pg]` (Postgres),
`.[orchestrator]` (Agent SDK + tracing), `.[durable]` (Temporal).

## 2. Build the notebook

```bash
ffelab build --src ~/re/ffed3d        # → ffe.db, docs/MODULE_MAP.md
ffelab graph build                    # call layers + call-graph embedding
ffelab graph check                    # how well the call graph predicts known labels
ffelab graph vote                     # adds the call graph's label hints to beliefs
```

The `graph` commands read only the call graph, so they run before any game run.
Their votes are hints, never proof: only experiments settle anything.
`ffelab similar FUNC --model callgraph-spectral-v1` lists the functions with
the most similar call neighbourhoods. Details: `docs/GRAPH.md`.

Then connect Claude Code through `.mcp.json` with `FFELAB_BACKEND=fake`
(GETTING_STARTED 4.3). You can already do static work: read blind views, make
calls, run propagation. **You:** record what you already know, with honest
confidences.

## 3. Check your game binary (do this first, before any emulator work)

```bash
ffelab exe-check /path/to/FIRSTENC.EXE
```

- `same_build`: the asm's addresses apply directly.
- `related_build`: ffelab stores an address map; look at `suspected_changed`.
- `different_layout`: wrong file; stop and sort that out.

Write down the `binary_id` it prints. **You:** get the right exe; nothing
below is trustworthy on the wrong build.

## 4. Emulator bring-up (one focused session)

This is the step that has never run for real, so expect fiddling
(PLAN "Emulator bring-up", GETTING_STARTED Stage 2):

1. Run FFE in QEMU + FreeDOS or DOSBox-X.
2. Calibrate `code_base`: at `code_base + Pos` of a known framed function you
   must see `55 8B EC` or `55 89 E5`.
3. Switch `.mcp.json` to `"FFELAB_BACKEND": "qemu"` and set `FFELAB_EMU`, e.g.
   `{"binary": "<binary_id>", "code_base": <n>, "gdb_port": 1234, "monitor_port": 4444}`.
4. Make and register 10–15 save states with tags, planned in pairs that differ
   by one tag.
5. Ask Claude Code for one logging pass and one knockout. If they work, you're live.

**You:** play the game to the save points; the agents can't make the first
states for you.

## 5. Start the servers you use (optional, Docker)

Skip this if you stay on SQLite with plain runs. Otherwise, with Docker Desktop
(or OrbStack) running:

```bash
cp .env.example .env              # once; pick services with COMPOSE_PROFILES=pg,temporal,tracing
docker compose up -d --wait       # starts them; `docker compose ps` should say healthy
set -a; source .env; set +a       # gives this shell FFELAB_DB etc.
```

Note that `.env.example` sets `FFELAB_DB` to Postgres. Comment that line out
if you want to stay on `ffe.db`. Details, ports and resets: `docs/DOCKER.md`.

## 6. The daily loop

Pick one of two ways to drive. Both write to the same ledger.

**A. Chat (Claude Code + MCP tools).** Best while you're learning and want to
watch. Give a goal and a time box, as in GETTING_STARTED Stage 3.

**B. Orchestrator.** Best once things work and you want agents to grind
without you:

```bash
pip install -e ".[orchestrator]"
ffelab agents run --worker heuristic --rounds 2           # free dry run first
ffelab agents run --backend qemu --max-usd 2 --trace phoenix --out reports/run1.json
```

`--trace phoenix` needs Phoenix running (Docker `tracing` profile, step 5);
the traces show up at http://localhost:6006. Leave the flag off to skip tracing.

It uses `FFELAB_EMU` for the emulator, and `ANTHROPIC_API_KEY` or your
logged-in Claude Code for the model. It stops on budget, on no progress, or when
10 questions are waiting for you.

**What you do each session, either way:**

1. **Answer questions.** In chat they come as you go; with the orchestrator:
   ```bash
   ffelab agents questions
   ffelab agents answer 7 "that save was mid-hyperspace" --close explained
   ffelab agents answer 9 "it draws the scanner" --claim ui/scanner --confidence 0.7
   ```
2. **Review contradictions** ("Show open contradictions" in chat). Close each
   as `explained` or `claim_withdrawn`; nothing gets named while one is open.
3. **Skim `knockouts/`**: the before/after/diff images are the strongest evidence.
4. **Make calls** when results remind you of something.
5. **Now and then, run `ffelab graph outliers`.** It lists calls between two
   labels that no other code supports, so each one is either a mislabel or a
   real cross-layer call worth a look. Re-run `ffelab graph vote` after labels change.

**Weekly:** "Show my calibration" in chat. **When names firm up:**
`ffelab export --src ~/re/ffed3d --out export`, review `export/ffe.asm.diff`.

## 7. Long runs: Temporal (optional)

Use it when a run is big enough that losing it to a hang would hurt.

```bash
pip install -e ".[durable]"                                    # server: Docker `temporal` profile, UI http://localhost:8233
python -m ffelab.workflows worker --db ffe.db --backend qemu \
    --emu '{"binary": "<binary_id>", "code_base": <n>}'
python -m ffelab.workflows start plan.json                     # in another terminal
python -m ffelab.workflows status NAME                         # or pause / resume / stop
```

`plan.json` format: GETTING_STARTED "Long runs" (skip its `brew install temporal`
and `temporal server start-dev` lines; the Docker container is that server). Try `--backend fake` first.
One worker per emulator; don't run the orchestrator against the same emulator
at the same time. **You:** write the plan, check status, review the results
the same way as step 6.

Without Temporal, plain runs (chat or `agents run`) do the same experiments;
they just don't resume after a crash.

## 8. Postgres (optional)

Switch when you run several agents at once (for example the orchestrator and a
Temporal worker together). Otherwise stay on SQLite.

```bash
pip install -e ".[pg]"                       # server: Docker `pg` profile (step 5)
ffelab db copy --from ffe.db --to "$FFELAB_DB"
ffelab embed && ffelab similar 688
```

Put the same `FFELAB_DB` URL in `.mcp.json` for Claude Code. (Installing
Postgres with Homebrew instead is in GETTING_STARTED 4.4.)

Every `ffelab` command, the Temporal worker and the MCP server read `FFELAB_DB`;
`--db` overrides it for one command.

## 9. Evals: when to run them

There is no CI; you run evals by hand (`docs/EVALS.md`).

- **Before keeping any change** to `.claude/agents/*.md` or the agents' model:
  ```bash
  ffelab eval run --dataset evals/datasets/smoke_blind.jsonl --out reports/new.json
  ffelab eval gate --report reports/new.json --baseline evals/baselines/blind-labeller.json
  ```
  It costs a little API money; `--solver heuristic` is a free check of the harness.
- **Once experiments have settled some labels** (after weeks of step 6):
  `ffelab eval export --db ffe.db --out evals/datasets`, run the agents on the
  real cases, and set real baselines with `ffelab eval baseline`. Until then the
  datasets are synthetic smoke tests and say little about the real game.

---

## A typical day, once everything works

1. `source .venv/bin/activate`; start the emulator; `docker compose up -d` and `set -a; source .env; set +a` if you use the servers.
2. Start a run: a chat goal in Claude Code, or `ffelab agents run --max-usd 2`.
3. Come back, answer `ffelab agents questions`, close contradictions, skim knockouts.
4. Make a few calls of your own.
5. If you touched an agent prompt: run the eval and gate it.
