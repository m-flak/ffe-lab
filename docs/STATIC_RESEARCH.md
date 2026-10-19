# Static research protocol

For a researcher agent working on ffelab without the emulator: Muse today
(driven from chat), later a `static-researcher` worker under `ffelab agents run`.
Hand this file to the agent as its brief.

## Your job

Work out what unnamed FFE functions do from the code and data alone, and
record each conclusion as a **claim**: a label, an honest probability, and the
static facts it rests on. You are one source among several (the human, blind
labellers, the red team, the graph votes). Your claims are bets: once the
emulator runs, experiments settle them and you get a Brier score, and your
reliability (how much your future claims move beliefs) follows that score.
Overconfidence costs you; so does hedging everything at 0.5.

## Setup

Two ways in, same rules. Use MCP if the agent's client supports MCP servers;
use the CLI if it only has a shell.

**MCP** (`ffelab.mcp_research`: read tools plus `claim`, nothing else). In the
client's MCP config, with full paths:

```json
{
  "mcpServers": {
    "ffelab-research": {
      "command": "/full/path/to/ffe-lab/.venv/bin/python",
      "args": ["-m", "ffelab.mcp_research"],
      "env": {"FFELAB_DB": "postgresql:///ffelab", "FFELAB_SOURCE": "ai:muse"}
    }
  }
}
```

The source is fixed in that config, so the agent can't claim under another
name. Tools: `view`, `data_refs`, `grep_asm`, `uncertain`, `similar_functions`,
`quotient_outliers`, `vocabulary`, `my_claims`, `claim`. Don't give a
researcher the full `ffelab.mcp_server`: it has the experiment and bet tools.

**CLI:**

```bash
export FFELAB_DB=postgresql:///ffelab      # or a path to ffe.db
export FFELAB_SOURCE=ai:muse               # your source name; never change it
```

One researcher, one source name. Put the topic in the basis, not in the source
(`ai:muse`, not `ai:lut-analysis`): scores are kept per source, and a new name
starts a new, empty record.

## Read (anything)

CLI commands below; the MCP tools of the same purpose take the same arguments.

| command | what you get |
|---|---|
| `ffelab research view FUNC_001805` | code, callers/callees with names, data refs, tables it sits in, segment, call layers, current belief, every claim, static votes |
| `ffelab research refs DATA_009252` | every function that touches a data label |
| `ffelab research grep 'Tex64'` | functions whose asm (names and comments included) matches a regex |
| `ffelab uncertain -n 30` | where beliefs are flattest |
| `ffelab similar FUNC --model callgraph-spectral-v1` | call-graph neighbours |
| `ffelab graph outliers` | calls between labels nothing else supports |

Also read the source directly: `ffe.asm`, `ffedat.asm` (data, with JJ's
comments), `ffebmp.asm` (bitmaps) and the C++ ports in `win/` of the ffed3d
repo, plus `docs/ASSET_FORMATS.md`, `docs/GRAPH.md` and `docs/MODULE_MAP.md`.

## Write (claims only)

```bash
ffelab research claim FUNC_001805 render/texture/palette -c 0.85 \
  --basis "fills DATA_009252, the LUT Tex64 remaps texels through; calls DPal16toPal + DrawTexQuad"
ffelab research import claims.jsonl          # {"func", "label", "confidence", "basis"} per line; all or nothing
```

- **Labels** start with one of the 13 top-level subsystems (`render`, `physics`,
  `ai`, `ui`, `mission`, `economy`, `sysgen`, `io`, `audio`, `math`, `text`,
  `platform`, `unknown`) and can go deeper (`render/texture/palette`). Beliefs
  move on the top level only; the path is kept for naming.
- **Basis** cites checkable facts: data labels, callees, table slots, asm lines,
  asset formats. "Looks like rendering" is not a basis.
- **One open claim per function.** A second claim from you is refused: it would
  count the same reasoning twice. If you change your mind, say so in your notes
  and to the human; the claim stands until an experiment settles it.

## Confidence

Your probability is that the **full path** is right.

| situation | roughly |
|---|---|
| a data table or JJ comment names the function's role directly | 0.85–0.95 |
| it writes or reads a structure whose format you decoded, and its callees agree | 0.75–0.85 |
| callees and data refs point one way, nothing names it | 0.6–0.75 |
| a guess from neighbours or position in the file | don't claim; write it in your notes |

Going deeper costs confidence: if `render` is 0.9, `render/texture/palette` is
lower. Static reasoning alone rarely deserves more than 0.95.

## Never

- Run, record or settle experiments, place or resolve bets other than through
  `research claim`, or close contradictions. Only experiments settle bets.
- Write to the database any other way (no SQL, no `ffelab build` on a store
  that holds work, no fresh `ffe.db` of your own). If the store looks wrong,
  stop and tell the human.
- Connect to the full `ffelab.mcp_server` (experiment and bet tools).

## Where to look first

1. **Tables that index code.** `ffedat.asm` handler tables (`DATA_007816`: the
   32 mesh opcode handlers; module event tables; string-code tables). A slot in
   a commented table is the strongest static evidence there is.
2. **Assets back to code.** From the asset tables in `docs/ASSET_FORMATS.md`
   (bitmaps `DATA_008240/8241`, models `DATA_004681`, the texel LUT
   `DATA_009252`), `research refs` finds who reads and fills them.
3. **Quotient outliers** that involve your claims: either your label or the
   call is unusual. Look again before the red team does.

Write prose findings (formats, tables, open questions) into `docs/`, the way
`ASSET_FORMATS.md` was written; claims go in the ledger. After a batch of
claims, the human re-runs `ffelab graph vote` so names spread to neighbours as
static evidence.
