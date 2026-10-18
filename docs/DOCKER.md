# Running the backing services in Docker

The stack PRs add three servers: Postgres with pgvector (shared ledger and
similarity search), a Temporal dev server (durable campaigns) and Arize
Phoenix (traces of agent runs). `docker-compose.yml` runs all three, so none
of them has to be installed on the Mac. Each one is optional. SQLite lite
mode (`ffe.db`) needs none of them.

## What runs where

| Where | What |
|---|---|
| Docker | Postgres 17 + pgvector, Temporal dev server, Phoenix |
| Mac | ffelab, Claude Code and its MCP server, QEMU or DOSBox-X with its GDB stub |

ffelab stays on the Mac on purpose. The emulator harness takes screenshots
with `screencapture` and sends keys through `osascript`, and it reaches the
QEMU monitor at a fixed `127.0.0.1:4444`. None of that works from inside a
Linux container, and Claude Code starts the MCP server as a local process
anyway. This matches D10 (Mac first): only the servers move into Docker.

All ports are bound to `127.0.0.1`, so nothing is reachable from your network.

## Setup

You need Docker Desktop (or OrbStack, or Colima) running.

```bash
cp .env.example .env              # once
docker compose up -d --wait       # starts the profiles listed in COMPOSE_PROFILES
docker compose ps                 # all three should say (healthy)
set -a; source .env; set +a       # puts FFELAB_DB etc. into this shell for ffelab
```

`.env` is gitignored. Compose reads it for the ports and profiles; ffelab only
sees it after the `source` line (add that line to your shell profile, or put
the same values in `.mcp.json`'s `env` block for Claude Code).

### Picking services

Profiles turn services on. Set `COMPOSE_PROFILES` in `.env`, or name them per
command:

| Profile | Service | Needed by |
|---|---|---|
| `pg` | Postgres + pgvector | `FFELAB_DB=postgresql://...`, `ffelab embed` / `similar` on pgvector |
| `temporal` | Temporal dev server | `python -m ffelab.workflows ...` |
| `tracing` | Phoenix | `ffelab agents run --trace phoenix` |

```bash
docker compose --profile pg up -d                       # just Postgres
docker compose --profile temporal --profile tracing up -d
```

With `COMPOSE_PROFILES` empty, `docker compose up` starts nothing.

## Using each service

**Postgres.** Install the extra and point ffelab at it:

```bash
pip install -e '.[pg]'
ffelab db copy --from ffe.db --to "$FFELAB_DB"    # move the existing ledger, ids kept
ffelab build ...                                  # every command now uses Postgres
```

The URL is `postgresql://ffelab:ffelab@localhost:5432/ffelab`. The pgvector
extension is already created, and a second database `ffelab_test` exists for
`FFELAB_TEST_PG`, so `python -m pytest` runs the storage tests on Postgres
without touching the real ledger. For `.mcp.json`, use the same URL as
`"FFELAB_DB"`.

**Temporal.** Use the commands from GETTING_STARTED's "Long runs" section but
skip `brew install temporal` and `temporal server start-dev`: the container is
that server, at the default `--address localhost:7233`. The web UI is at
http://localhost:8233. Workflow history lives in the `temporal` volume, so a
campaign survives `docker compose restart`.

`tests/test_workflows.py` starts its own throwaway Temporal server and does not
use the container. It needs the `temporal` binary on PATH (or
`TEMPORAL_CLI`), else the SDK downloads one; if that fails the tests skip.

**Phoenix.** `pip install -e '.[orchestrator]'`, then
`ffelab agents run --trace phoenix ...`. Traces go to
`PHOENIX_COLLECTOR_ENDPOINT` (`http://localhost:6006`, the default) and show up
in the UI at the same address, under the `default` project.

Langfuse is not included. Its self-hosted setup needs ClickHouse, Redis and
object storage on top of Postgres, which is a lot to run for an optional
second tracing UI; Langfuse cloud with `LANGFUSE_HOST` and the two keys works
without any of it.

## Ports already taken

If Postgres (or anything else) already listens on one of the ports, change it
in `.env`, e.g. `FFELAB_PG_PORT=5433`, update the matching URL in the same file
(`FFELAB_DB`, `FFELAB_TEST_PG`), and run `docker compose up -d` again. For
Temporal, pass the new port as `--address localhost:PORT`.

## Data, reset and upgrades

Data lives in named volumes (`ffelab_pgdata`, `ffelab_temporal`,
`ffelab_phoenix`), not in the repo.

```bash
docker compose stop               # stop, keep data
docker compose down               # remove containers, keep data
docker compose down -v            # remove containers AND data (the Postgres ledger too)
docker compose logs -f postgres   # what a service is doing
```

`docker/postgres-init.sql` only runs when the Postgres volume is first created.
Image versions are pinned in `docker-compose.yml`; to upgrade Postgres to a new
major version, copy the ledger out first with
`ffelab db copy --from "$FFELAB_DB" --to backup.db`, since a new major version
can't open the old data directory. Embeddings are not copied; rerun
`ffelab embed` afterwards.
