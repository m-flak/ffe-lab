# ANISO comment survey (static research notes)

279 claims filed from Anisotropic's in-asm comments (ffe.asm: 143, ffedat.asm: 23,
ffebmp.asm: 0). Source: `ai:muse`. This file records method, judgment calls and
what was deliberately NOT claimed.

## Method

- Prelude comments (line starts with `;`, 1-2 lines above a `FUNC_` label)
  describe the NEXT function. Verified on ~90 cases; the window/logic split
  below cross-validates the attribution (windows land in the table, preluded
  logic lands outside it, with no exceptions found).
- Inline/trailing comments describe the enclosing function; call-site notes
  (`call FUNC_000237 ; fine player...`) describe the callee.
- Every claim basis cites file + line so it can be re-checked.

## Key structural finding: windows in tables, logic preluded

`DATA_005288` (ffedat.asm:6650-6767, 95 unique members) holds window procedures;
adjacent ANISO-preluded functions hold the logic:

| window (in table) | logic (preluded, not in table) |
|---|---|
| 382 market prices | 383 sets prices by tech level |
| 436 ship prices | 437 sets ship prices (x1000) |
| 554 stockmarket | 555 buy 1t / 556 sell 1t |
| 478-481 crew offers | 482 accept/decline + capacity |
| 548-550 bribery | 551 bribery resolution (calls 237) |
| 460/467/469 assassination | 290 generate, 498 bombing accept |

Same pattern in AI: `DATA_006255` (ffedat.asm:10029-10179, states 0x0-0x24,
51 claimed members) holds per-sec/per-frame state funcs; 706 is its sole
dispatcher (only referrer of the table base).

## Judgment calls (top-level ambiguity, flagged in bases)

- Crime/police cluster (233 contraband, 236 respond, 237 fine, 551 bribery,
  2110 piracy/murder detect) filed under `economy/police/*`: fines/bribery are
  credit flows tied to trade and BBS payments. `mission` (legal career, sibling
  to ranks) is the main alternative; `ai` (dispatch) second. Revisit if the
  red team disagrees.
- 236/237 will likely show as quotient outliers (callees vote audio/ui from
  incidental sound/message outputs). The fine-ledger writes + offense table
  are the real behavior.
- 903 hyperjump range/fuel calc filed `sysgen` (inter-system travel) over
  `physics` (ship performance math); genuinely close, confidence 0.6.
- 714 scooping filed `ai/cargo/scoop` (AI-table state 0x17 per-frame) over
  `physics`: the table shows the engine treats items as AI-driven objects
  (cf. missiles 0x13, mines 0x14).

## DB quirk (do not "fix" by hand)

- DB `FUNC_000859` merges two asm functions: the 10-instr 852-wrapper AND the
  numberless `_FUNC_GetSysGenParams` body (ffe.asm:61838). Claimed as
  `sysgen/params/fill` (the dominant body); basis notes the merge.
- `FUNC_000772` has no `FUNC_:` label (only `_FUNC_000772_AIShipSpawn:`) but
  the DB does carry it; claimed `ai/spawn`.

## Deliberately not claimed (notes only)

- 1538 GetModelPtr: 6-instr universal lookup (`DATA_004681[model]`), 58 callers
  across subsystems. Role certain (ANISO 82905), top-level unresolvable
  statically (physics/render/math/platform all live). Needs experiment or
  taxonomy decision, not a guess.
- 843 (exports/imports display: ui/sysgen/economy 3-way), 1125 ('use camera?':
  render/ui/equipment), 154 (3400-line load-dialog giant), 919 (splash + aging
  + new-days mix), 1014 (cabin temp), 1021 (velocity fudge, commented out),
  275 ('revised'), 301 (flag cleanup?), 938 (disabled shield-regen block),
  692 (neighbor-only evidence).
- DATA_005300 ('indexed by drive'): zero code references found; possibly dead
  or reached via computed base. DATA_005039 offense blocs beyond index 8
  undecoded (fines 9/10 observed at call sites).

## Open threads for follow-up

- Decode full DATA_005039 offense list (indices 0-10+); pins 233/246/377/542/
  790/1023/1094 fine contexts.
- 386 (outfitting window) should show prices from DATA_005289 and charge
  credits; verify to firm up the economy/outfitting top-level.
- 899/900/901 (callers of 903) likely galaxy-map UI + AI jump planning.
- Non-ANISO commenters ('BBS Related??', 'dunno what this is...') and the 124
  committed `_FUNC_*` aliases are a second vein; only a few were used here
  as corroboration.
