# Model type record (static research notes)

`Model_t` (the "model", more precisely a per-type archetype record) lives
behind the 465-pointer ship table at `DATA_004681` (ffebmp.asm:41844):
`GetModelPtr(i)` = `table[i]`. Each record has a geometry half (mesh
pointers, read by render through paths invisible to call-site taint — see
ASSET_FORMATS.md) and a stats half (pointed to from +0x38, read densely by
physics/UI/AI). Fully read-only: 80 call sites, zero confirmed writes.
`sizes below are lower bounds; render may read further into the geometry half.

```c
#include <stdint.h>

/* Game is 32-bit: pointers are 4 bytes. The host compile check is 64-bit,
   so pointer fields use this instead of real pointer types. A plain
   uint32_t is always a value, never an address. */
typedef uint32_t u32pointer;

typedef struct __attribute__((packed)) {
    int16_t  len;           /* +0x00: extent/length -> actor+0xc4 (036) [M] */
    int16_t  extent;        /* +0x02: bounds -> actor 0xbc-0xc6 block (036);
                              data always <= 0 (68 neg, 16 zero): 036
                              negates to magnitude, class vs 0xaa5/0x1329 [M+] */
    uint16_t caps;          /* +0x04: bit8 laser-compat (394); lo byte ->
                              actor drive_sub (036) [M+] */
    int16_t  mass;          /* +0x06: hyperjump range divisor (903); classes
                              0xa/0x190 (942); hull = 4x (036) [M+] */
    int16_t  capacity;      /* +0x08: qty = capacity - DriveTable (036);
                              DestroyEquip recompute (952) [M+] */
    int16_t  price_b;       /* +0x0a: ship-price input (437/444) [L] */
    int16_t  s0c;           /* +0x0c: (284/559) [L] */
    int16_t  variant;       /* +0x0e: subtype; display (316), pricing (437),
                              naming (791), windows (283/470) [M] */
    int16_t  s10;           /* +0x10: (439/441/999) [L] */
    int16_t  mounts;        /* +0x12: equipment-mount base; indexes actor
                              +0xd6 array (418/419/498/714) [M+] */
    int16_t  drive;         /* +0x14: stock drive class: 0 never occurs in
                              data (runtime-removed?); 26 models <0 fixed-
                              install (036), 58 >0 skip; outfitting checks
                              (410/411) [M+] */
    int16_t  bounty;        /* +0x16: combat-rank value (942 only) [M] */
    int16_t  s18;           /* +0x18: spawn offset scalar: 1070 sets actor
                              pos36 = arg - (S<<3); 1053 same [M+] */
    int16_t  s1a;           /* +0x1a: spawn offset scalar (pos2e side);
                              1070/1053 [M+] */
    int16_t  s1c;           /* +0x1c: live data (-937..28672), no tainted
                              readers; NOT padding [?] */
    int16_t  s1e;           /* +0x1e: live data (0..2500), readers unknown [?] */
    int16_t  laser[4][2];   /* +0x20-0x2e: per-mount pairs read by fire
                              logic (931); 1776 corroborates first pair [M] */
} ModelStats;
_Static_assert(sizeof(ModelStats) == 0x30, "stats exact: all 84 spans 48");

typedef struct __attribute__((packed)) {
    u32pointer mesh;        /* +0x00: display-list stream [H: ASSET_FORMATS] */
    u32pointer verts;       /* +0x04: vertex pool (579/580/819/820) [H] */
    uint32_t numverts;      /* +0x08 [H: ASSET_FORMATS] */
    u32pointer normals;     /* +0x0c (579/580/819/820) [H] */
    uint8_t  _u10[4];       /* +0x10: no attributed reads [?] */
    uint32_t dim_a;         /* +0x14: dimension: dim-sums (036/579),
                              shift+limit (1056) [M, multi-use] */
    uint32_t dim_b;         /* +0x18: dimension, added with dim_a [M] */
    uint32_t eng;           /* +0x1c: engine/input scale: 1056 clamp,
                              1110, 1680/1750 [M] */
    uint8_t  _u20[4];       /* +0x20: (1680 only) [L] */
    uint8_t  fl24;           /* +0x24: bits 4-6 -> actor flags (036) [M] */
    uint8_t  _u25[1];        /* +0x25 */
    uint8_t  b26;            /* +0x26: (1680 only) [L] */
    uint8_t  _u27[1];        /* +0x27 */
    uint32_t d28;            /* +0x28: compared (1680) [L] */
    uint32_t scbase_a;       /* +0x2c: scaled by 036 (low word) into
                              actor sc138 [M] */
    uint16_t scbase_b;       /* +0x30: scaled by 036 into actor sc140 [M] */
    uint8_t  _u32[2];        /* +0x32 */
    u32pointer tag;          /* +0x34: 8-byte descriptor block, NOT a string
                              (e.g. 10 80 02 03 04 06 00 00); all 465 set,
                              164 with nonzero first byte -> actor flags
                              0x80 (036); read for display (576/579/580/
                              819/820) [M] */
    u32pointer stats;       /* +0x38: -> ModelStats; 84/465 set (indices 4-71
                              + 16 strays), rest NULL; NULL-checked
                              (037/275/942); some models
                              (scenery?) carry no stats [H] */
} Model;
_Static_assert(sizeof(Model) == 0x3c, "fixed header; variable tail follows");
```

## The initializer (036) and scaler (1495)

FUNC_000036 (`physics/spawn/init_actor`, 0.7) builds an actor from a model:
bounds, hull (mass×4), drive, capacity, flags, scaled constants. 1495
(`math/fixed/scale`, 0.75) is its fixed-point scaler: `dest[8] =
(int64)value << (shift-15)`, via Int64ArithShift. 036 packs (value, shift)
into one dword push — the dim-sum store that looks vestigial is the shift.

## Data check (all 465 models / 84 stats blocks)

mesh/verts/numverts set on all 465; normals on 253 (212 NULL); tag
pointers all set (301 empty, 164 nonempty first byte); stats on 84.
Stats value ranges (min..max): len 0..32700, extent -13625..0, caps
0..260 (bit8 present), mass 1..16383, cap 0..15000, price_b 11..16394,
s0c 40..800, variant 0..16592, s10 1..100 (percent-like?), mounts 0..10,
drive -32767..3331, bounty 0..256, s18 0..28672, s1a 0..1875, s1c
-937..28672, s1e 0..2500, laser pairs mixed-sign except las00 (36..31250)
and las21 (3..2250) always positive. Record tails vary past 0x3c
(model 0: long dd-run; model 4: 8 zero bytes) — face tables live there.

## Open gaps

_u10, _u1c (stats hole), 1680's geometry block (0x20-0x28: bounding query?),
s10/s18/s1a/s0c/s0a semantics, laser pair meaning (damage/range? offset?),
which models lack stats, bounty single-witness (942).
