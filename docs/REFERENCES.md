# References: Frontier: Elite II (glFrontier)

`ffelab ref import` matches FFE functions to Frontier: Elite II's 68k code
(`fe2ref.py`, D17). The analysis below was first done in
`analysis/glfrontier/` in the project folder; the numbers are from that run on
`crinkelitehttps/glfrontier-extended` b47c7b6 (`fe2/fe2.s`) and ffed3d 8b6df7c.

```
ffelab ref import --fe2 PATH/TO/fe2.s --src PATH/TO/ffed3d   # writes ref_matches, prints a hold-out check
ffelab ref show [FUNC]
```

## What glFrontier actually is

glFrontier / FrontVM (Tom Morton) is **not** a hand translation of Frontier into C. It is
a 68k assembler (`as68k`) that compiles a labelled disassembly of the Atari ST
`frontier.prg`, called **`fe2.s`**, into one huge generated C file (or i386 asm), plus a
small host layer (GL renderer, input, audio). The useful thing is `fe2.s`: about 66k lines
of 68k with Tom Morton's labels and notes.

- `pcercuei/glfrontier` (commit 0a09ada) deliberately leaves `fe2.s` out for copyright reasons.
- Forks that ship it: `crinkelitehttps/glfrontier-extended` (b47c7b6, used here: 391 named
  labels; `fe2_modded.s` has 474), `SomerledDesign/final-frontier` (e17cad4),
  `Kochise/GLFrontier-win32` (9ecb7c3). Each has 329 named labels.
- What it gives you: about 1,000 code blocks, 241 of them named (e.g. `L368e0_Sqrt`,
  `L45900_2RandInts`, `mod1_fn24_UpdateSystemPhysics`), about 150 named game-state offsets
  (`A6_rng_seed1`, `A6_opt_*`, keystates), the a5 jump table, and notes (ship type ids,
  `FmtStr` codes 0x3001-0x3015, 0x4xxx string ids, 0x80xx-0xa2xx module string ids,
  module slot 24 = per tick, slot 28 = per second).

## How far it lines up with FFE

FFE is **not** instruction-level copy-paste of the 68k. Matched pairs implement the same
algorithm with the same constants and tables, but the x86 looks compiled or rewritten. For
example, FE2's `2RandInts` (`eori #$1234567f; ror #7; ...; swap`) is FFE `FUNC_001530`
(`xor 0x1234567f`, then the rotate written as `shl 0x19 / shr 7 / or`). And `Sqrt`
(FFE `FUNC_001467`) uses the same 4 KB lookup table byte for byte, with different branching.
So the reuse is real at the level of design, data and constants, which means instruction
n-gram matching won't work. Matching has to use what survives a rewrite:

| signal | result |
|---|---|
| **Strings** | 1,506 of FE2's 2,207 strings (≥5 chars) also occur in `ffedat.asm` |
| **Data tables** (big-endian to little-endian, word to dword) | 27 FE2 tables found in `ffedat.asm`: sqrt, inverse, static system positions, others. 15 of them give a unique function pair |
| **Distinctive constants** (TF-IDF cosine, mutual best) | 74 strong pairs. Callee consistency is 25-30%, against 0.2% for a shuffled pairing |
| **Call-graph propagation** from those anchors (BinDiff-style: pair unmatched neighbours by constants, size, degree) | +76 pairs, **160 in total** |

**Hold-out check:** hide 30% of the anchors, propagate from the rest, and see whether
the hidden anchors come back right. Recall 30%, precision 86% (5 repeats). This is
precision against other static anchors, not against experiments.

**Name check:** for the 8 matches where both sides already had a name, 5 make sense
(`SysMenuUpdateGfxDetail`/`SetDetailOpts`, `SysMenuProcessAction_Event4`/`DoOptChange`,
`SysMenuInit`/`mod10_fn4`, `GuiReadAllInput`/`UpdateMouse`, `MatBuildYZT`/`MakeRotXYMatrix`)
and 3 don't (`CdrFileDispErrorCode`/`NameToUpperCase`, and `SysCleanup`/`SysExit` matched
to mouse routines). The wrong ones are weak matches: one constant, or propagated with no
constant support (`csim` 0).

**Coverage:** 160 of 1,614 FFE functions (10%). 23 of them are unnamed in FFE and get an
FE2 name, e.g. `FUNC_001467`←Sqrt, `FUNC_001530`←2RandInts, `FUNC_000865`←UpdateSystemPhysics,
`FUNC_001036`←GenAsteroids, `FUNC_001038`←PlayerControlUpdate, `FUNC_000326`←PoliceConfiscateShip,
`FUNC_001114`←DrawLineClipped, `FUNC_001546`←DrawEllipse. The ceiling is FE2's own size and
naming: about 1,000 blocks and 241 names.

In `ref_matches` (`how` = data / const / propagated; `csim` = constant similarity;
treat `csim` < 0.3 propagated pairs as weak).

## The galaxy test (is the platform/simulation split authorship?)

Share of each half matched to FE2 (galaxy split rebuilt as in `docs/GRAPH.md`; family mix
reproduced):

| half | functions | matched | anchors only (no graph use) |
|---|---|---|---|
| platform/UI | 706 | 70 (9.9%) | 35 (5.0%) |
| simulation/maths | 823 | 90 (10.9%) | 49 (6.0%) |

**No meaningful difference.** If the maths half were Braben's ported 68k and the platform
half new x86, the maths half should match far more often. A twofold gap would show at these
counts, but it isn't there. The platform half's matches are the 2D/3D drawing and projection
primitives and the options menu, which FE2 has too. The 64-bit maths families (`Int64`,
`Vec64`) match nothing, as you'd expect for code FFE added for 32-bit x86. So the reading is
that both halves carry FE2 lineage about equally, and the split is subsystem (UI vs maths),
not author. Confidence: moderate. Recall is only about 10%, so a small difference could hide.
This is static evidence and settles nothing.

## How it is used (ledger rules)

1. **Reference facts, not evidence (for now).** Matches go to `ref_matches` with a
   confidence by how they were found: data or const anchors 0.8, propagated with `csim`
   ≥ 0.3 0.7, propagated with `csim` < 0.3 (`propagated_weak`) 0.45. They don't touch
   beliefs, bets or contradictions. FE2 names aren't subsystem labels. With a vetted
   FE2-name to subsystem map they could become `static_ref` evidence (in `STATIC_KINDS`,
   with its own source reliability).
2. **Labeller hints, not the blind labeller.** The MCP `belief` tool shows a function's
   `ref_matches`. `blind_view` does not, since they carry other people's names (same
   rule as the graph label vote).
3. **Not eval gold** (D12: gold only from experiments). It can be a diagnostic column
   ("agrees with FE2 match") or a source of hypotheses for experiments: ship type ids,
   `FmtStr` codes, and module slots 24/28 give concrete things to break-point.
4. Optional later: running glFrontier itself as a behavioural oracle (same scenario in FE2
   and FFE), which is a bigger project.

**Licensing:** `fe2.s` is a disassembly of a copyrighted game, the same status as `ffe.asm`.
pcercuei's README won't distribute it, and the forks that do have no clear right to.
glFrontier's own code is described as "GPL (i guess...)" with no licence file; the Kochise
fork says MIT. So don't vendor `fe2.s` or any glFrontier code into ffe-lab. Read it from a
user-supplied path (as `--src` does for `ffe.asm`) and store only derived facts (pairs,
names, scores) in `ffe.db`. `fe2ref.py` is original (our own 68k parser), so it carries no GPL obligations.
