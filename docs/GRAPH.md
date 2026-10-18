# Call-graph structure

What the shape of the call graph says about FFE, and how ffelab uses it
(`graph.py`, D16). Everything here is static: built from the `calls` table,
before any game run. Numbers are from the real `ffe.asm` (1,614 functions,
3,805 call edges between them).

```
ffelab graph build      # call layers + spectral basis (embedding callgraph-spectral-v1)
ffelab graph check      # hold-out accuracy of the votes below
ffelab graph vote       # write static_graph_knn and static_quotient evidence
ffelab graph outliers   # calls between labels nothing else supports
ffelab similar FUNC --model callgraph-spectral-v1
```

MCP: `similar_functions(func, model="callgraph-spectral-v1")`, `graph_votes`,
`quotient_outliers`; `blind_view` now includes `call_layers`.

## Three views

**Spectral basis.** The graph Laplacian is a matrix with one row per function.
Its eigenvectors form a basis for "one number per function", ordered from
smoothest (changes least across call edges) to roughest, much like a Fourier
series. A function's coordinates are its entries in the 32 smoothest ones, so
functions that call each other, directly or through shared partners, land
close together. No names or addresses go in.

**Call layers.** The graph is not a DAG: 18 functions call themselves and 8
groups call each other in cycles. Collapse each cycle to one node and it is.
`height` is the longest call chain below a function (0 = calls nothing),
`depth` the longest chain above it. Maximum height is 21.

**Quotient.** Collapse every function with a known label to its label and you
get a small label-to-label call graph. A labelling that respects it is a graph
homomorphism: every call maps onto a label pair the rest of the code also uses.

## What was measured

How often a function's 5 nearest neighbours share its inherited name family
(16 families, 243 named functions; chance 12%):

| space | agreement |
|---|---|
| hand-picked axes (fan-in, fan-out, link-order distance) | 34% |
| code n-gram embedding (`asm-ngram-v1`), 3 principal components | 38% |
| code n-gram embedding, all 256 numbers | 50% |
| spectral basis, 3 dimensions | 54% |
| spectral basis, 32 dimensions (z-scored, cosine: the stored form) | 63% |
| link-order neighbours (for reference; this is the segmenter's signal) | 86% |

Weighting edges by call sites, or encoding direction (a magnetic Laplacian),
scored no better (57% vs 55% at 16 dimensions). Adding the spectral basis as a
segmenter boundary feature moved cross-validated AUC by -0.04 to +0.01 across
seeds: noise, so the segmenter is unchanged.

`ffelab graph check` on the 13 top-level labels (241 functions labelled by
inherited name families; each one's label hidden in turn):

- neighbour vote picks the true label **78%** of the time (most common label: 26%);
- the quotient still allows the true label **97%** of the time, while ruling out
  all but a median of **2** of 13 labels.

The 16 spectral dimensions explain 52% of link order (R²) without ever seeing an
address: the call graph alone rediscovers much of the file layout, independent
support for D2.

## What the shape shows

- **Two halves.** The basis splits the code into a platform/UI half (String,
  SysMenu, Draw, CdrFile, Gui, Blit, Tex) at the start and end of the binary and
  a simulation/maths half (Int64, Vec64, Mat, Spawn, Sound) in the middle, as long
  contiguous runs (208 runs along link order; a random split gives ~760). The
  halves differ slightly in style (frames 97% vs 92%, fewer 16-bit registers and
  data refs in the maths half), but subsystem and author are confounded here:
  "Sawyer's x86 platform vs Braben's 68k-translated simulation" and "UI vs maths"
  predict the same picture. Deciding test: match the maths half against a
  Frontier: Elite II (Amiga, 68k) disassembly.
- **One-way layers.** Of 210 possible cross-family call directions, named code
  uses 18, and only SysMenu/CdrFile call each other both ways. 69% of named calls
  stay inside a family.
- **The text engine is the knot.** The 110-function cycle runs through the
  string expander (`StringExpand*`, `ExpandBracketCodes`) and out into game code
  (`GenerateName`, `GetSysGenParams`, `GetSysObjectData`, `SpawnCargo`,
  `TakeDamage`, `ShowCommMessage`, `BeginEvents`) and back: string codes appear
  to call into the simulation for values, which formats messages by calling the
  expander again. It spans both halves. Inferred from static edges; a halt in
  `StringExpandFFCode` watching the callback chain would confirm it.
- **String utilities are one module.** All 61 String-family functions are one
  contiguous block (link order 1106-1166); the islands in the plot are separate
  call clusters inside it, not rival libraries. Matching every String function
  against code outside the block finds nothing closer than for random blocks.

## How it is used, and the rules

- Static evidence only (`static_graph_knn`, `static_quotient`, both in
  `STATIC_KINDS`): it shapes beliefs and which experiment to run, never settles a
  claim, never raises a contradiction, and a re-run replaces the last one.
- Votes come from labels known without static evidence (`propagate.callee_vote`:
  experiments, then confident non-static beliefs, then inherited name families),
  so they can't echo through the graph.
- Quotient outliers are candidates for review (red team, contradictions), not
  verdicts: each is a mislabel or a real cross-layer call.
- Blind labeller: `similar_functions` with `callgraph-spectral-v1` returns
  numbers and scores only, and `call_layers` in `blind_view` is structure only,
  both derived from the caller/callee lists `blind_view` already shows. The
  neighbour *label* vote uses other people's labels, so it must never be shown
  to the blind labeller.
- Static analysis finds groups; running the game names them. Name one or two
  members of an unnamed group by experiment and the votes carry the name to the
  rest as bets. A group's place in the quotient (who calls it, its layer) tells
  you which scenario should make it fire.
