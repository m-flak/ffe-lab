# Actor object layout (static research notes)

We think this is the world actor object. It looks like it could be, but we don't really know.
I'm calling it the actor object for now. Hopefully, the future can discern this object better.
Take this file with a grain of salt and beware of hallucination! 

World actors (ships, stations, missiles, cargo, clouds — and the player, who
is just the slot indexed by `DATA_008857`) are POD records in a fixed arena.
Allocator: FUNC_000928, reached through the hook pointer `DATA_007219`
(annotated at every call site); construction runs through a second hook,
`DATA_007755`. No malloc anywhere. Field confidence: [H]igh / [M]edium /
[L]ow / [?] open.

Attribution method: taint analysis over all of ffe.asm. A register counts as
an actor pointer if it touches 2+ proven fields (or comes from the allocator,
`DATA_008861`, or `[game+0xc8]`); 125/338 offsets attributed. Blind spots,
all since verified with direct lines: base+index addressing (498/714 index
mounts), singleton writers the 2-touch rule drops (394/722/738/689/690/930/
941/413/415/598/599/600/606), plain `[ebx]` derefs, and values laundered
through the stack — including whole-snapshot access: 1054 snapshots slots to
the stack and 1052/1053/1679/1070/1682 read and write the actor-layout image
there, invisible to slot-based taint. [M+]/init-seeded fields come from the
type side; see MODEL_RECORD.md (036/1533 init, 1495 scaler, stats data).

```c
#include <stdint.h>

#define POOL_N    114
/* Pool base is indirect: [DATA_009133] holds Pool*, cached in [DATA_007758]
   (19532/19726 copy 9133->7758; other paths take base as arg). 1531 proves
   the stride: slot i at POOL+0x74+0x152*i. */
#define POOL_PTR   (*(uint8_t **)DATA_007758)

/* Slot flags: index i (1-based) at POOL[0x01 + (i-1)].
 * Flag value = kind byte (0x0e station, ...); bit 0x20 = evictable.
 * DATA_008834 counts live evictable slots (928 inc/decs it). */
static inline uint8_t *slot_flags(void) { return POOL_PTR + 0x01; }

typedef struct __attribute__((packed)) Actor {   /* the world actor record */
    int32_t  u00;           /* +0x00: unknown; no literal game touches (the two
                              +0x0 lines file-wide are _LongJmp's context;
                              plain-deref access possible) [?] */
    int32_t  mat[8];        /* +0x04-0x23: orientation-matrix rows. 1053 reads
                              0x0c-0x23 as fixed-point (sar 0x10) for thrust
                              decomposition; thrust (601), turning (585),
                              fire (931), missile AI (754) read; 650 writes;
                              732 byte-compares +0x08 (union or bleed?) [H] */
    uint8_t  b24;           /* +0x24: only witness 1736 (suspect) [?] */
    uint8_t  b25;           /* +0x25: zeroed (597/728/996/1088/1533) [M] */
    /* position block: 1070 writes each as a (value,0) dword pair; 862 writes
       the low halves; 1683 reads all three. Second position set alongside
       pos[3] below — frame/role open (render echo? parent frame?). [H] */
    int64_t  pos26;         /* +0x26: = spawn arg (1070) */
    int64_t  pos2e;         /* +0x2e: = arg - (S+0x1a<<3) (1070) */
    int64_t  pos36;         /* +0x36: = arg - (S+0x18<<3) (1070) */
    int64_t  pos[3];        /* +0x3e,+0x46,+0x4e: live POSITION. 1052 Int64Adds
                              1053's thrust decomposition into them; 605
                              FOR-find pushes hi/lo halves for distance;
                              441-cluster reads [H] */
    uint8_t  for_id;        /* +0x56: frame-of-reference slot index. 1679 feeds
                              it to 1531 (slot_at) for the relative matrix [H] */
    uint8_t  for_sub;       /* +0x57: FOR sub-id, 0/1 (239/243); 1054 gates
                              1679 on nonzero [H] */
    uint8_t  f58;           /* +0x58: matrix cache key vs DATA_008811 (1679
                              recomputes on mismatch); spawn-zeroed (297/728) [H] */
    uint8_t  _u59[1];       /* +0x59 */
    int32_t  m5a[3][3];     /* +0x5a-0x7d: cached orientation matrix, 1679's
                              MatMatMul output (inputs: b56 frame via 1531);
                              570 touches m[2][2]-high [H] */
    int32_t  counter;       /* +0x7e: countdown presets 10/6 (722), 8 (738);
                              inc/dec (942), dword (740), 939/1035/1094 [M] */
    int32_t  model;         /* +0x82: model id; GetModelPtr arg; 0x45/0x4F =
                              police; 036 stores, 1070 R/W, 36-func map [H] */
    uint8_t  slot;          /* +0x86: OWN slot index (38-func readership), but
                              NOT stamped by 928 (it only counts) — stamper
                              unknown [M] */
    uint8_t  kind;          /* +0x87: entity kind: 1, 0x0b cargo, 0x11 ship,
                              0x0e station (mechanism open) [M] */
    uint8_t  variant;       /* +0x88: variant byte; 930 reads for sound pick,
                              942 reads [H] */
    uint8_t  _u89[2];       /* +0x89-0x8a (0x89 dead file-wide) */
    uint8_t  b8b;           /* +0x8b: zeroed (725/727/728 laser) [M] */
    int32_t  vel[3];        /* +0x8c,+0x90,+0x94: VELOCITY; thrust adds (601);
                              flight/tick/FOR cluster (575/749/605); 291
                              subtracts at 0x8c [H] */
    uint8_t  _u98[4];       /* +0x98-0x9b (0x98/0x9a touched, unattributed) */
    uint16_t w9c;           /* +0x9c: = game+0>>10 (1070) [M] */
    uint16_t cargo_type;    /* +0x9e: commodity type, 0xffff none; +0x8e00
                              scooping message (714, ANISO-confirmed);
                              free-space check; spawn arg (1070) [H] */
    int32_t  param;          /* +0xa0: dword param (297 station arg); copied
                              from game+0xcc slot (1070) [M] */
    int32_t  ticks;          /* +0xa4: counter; clouds init 0xffffffff (942);
                              spawn = !(flg14c & 0x20) (1070) [M] */
    int32_t  t1, t2;        /* +0xa8,+0xac: spawn-set pair, inc/add (689/690);
                              733-cluster reads [L] */
    int16_t  rot_x;         /* +0xb0: x-rotation [H: 585 `; xrot`] */
    int16_t  rot_y;         /* +0xb2: probable y-rotation (585) [M] */
    int16_t  rot_z;         /* +0xb4: probable z-rotation (585) [M] */
    uint16_t throttle;      /* +0xb6: zero-init (597/750/909/1042/1098);
                              thrust reads (601) [M] */
    uint16_t w_b8;          /* +0xb8: same cluster, thrust-adjacent [M] */
    uint16_t w_ba;          /* +0xba: same cluster [M] */
    /* bounds: 036 copies S+0x02 extent: +words to c4/c6/c2/be, NEGATED to
       c0/bc; 0xbc magic-checked vs 0xaa5/0x1329 (036/1533 size class); 449
       (laser init) overwrites c0/c2/c4 with laser cells; flight
       (598/599/600) movsx-reads c4; be movsx-read (441/597/919/996) [H] */
    int16_t  ext0;          /* +0xbc: extent, negated */
    int16_t  ext1;          /* +0xbe: extent */
    union {
        struct { int16_t ext2, ext2s, ext4; };  /* init extents; ext4 = S+0x00 */
        struct { int16_t tol, tol_m, len_m; };  /* laser cells (449) */
    } c0;                   /* +0xc0-0xc5 */
    int16_t  ext5;          /* +0xc6: extent (036; also 441/449/986/996).
                              NOTE: game-state+0xc6 (DATA_008804, NOT this
                              struct) carries ANISO's illegal-shooting flag
                              from 301 (bit2; bit4 tested via 253) [H] */
    /* equipment bits: 441 inits c8-cb as one dword; 394 dword-ANDs it;
       sub-bytes are bit-tested [H] */
    uint8_t  c8;            /* +0xc8 */
    uint8_t  c9;            /* +0xc9: bit2 = fuel scoop (714) */
    uint8_t  ca;            /* +0xca: bit2 (272), bit6 = bounty (942) */
    uint8_t  cb;            /* +0xcb: bit2 tested (937) */
    uint16_t wcc;           /* +0xcc: bit2 cleared by bombing-accept (498:
                              and -0x5); 441 dword-init covers cc-cf [M] */
    uint16_t wce;           /* +0xce: high half zeroed by 297; movsx (650) [M] */
    uint8_t  drive;         /* +0xd0: drive class: 0 none, <10 std, >=10 mil;
                              036 installs S+0x14 low byte if <0; 948 R/W [H] */
    uint8_t  drive_sub;     /* +0xd1: = S+0x04 lo (036/1533); ==0/4 (283),
                              ==2 (1001/1056) [H] */
    uint8_t  d2;            /* +0xd2: zeroed (441/1098; 1098 briefly 0x88),
                              (2110) [M] */
    uint8_t  d3;            /* +0xd3: zeroed (441/1098) [M] */
    uint8_t  d4;            /* +0xd4: zeroed (441/1098) [M] */
    uint8_t  d5;            /* +0xd5: zeroed (441/1098) [M] */
    /* 441 lea-passes +0xd6; 1098 zeroes all 10; S+0x12 (0..10) is the fitted
       count; 498/714 index it base+index (bombing 0x89, display 0x88/0xc0);
       239 inc-word@0xdc; bytes 4:5 = range word (FOR-find 606) [H] */
    uint8_t  mounts[10];    /* +0xd6-0xdf */
    int16_t  shields;       /* +0xe0: current shields (1099 display; 441/1098/
                              1533 zero; 938's reads are commented out) [H] */
    int16_t  shields_max;   /* +0xe2: +0x40 per unit (413 install / 415 rm);
                              read (283, 1099) [H] */
    int16_t  hull;          /* +0xe4: hull, = S+0x06<<2 at init (036/1533);
                              941 flee-check, 1099 x8 display, 283 read [H] */
    uint16_t sh_x;          /* +0xe6: 0xffff init (931 fire); read (681/740/
                              1094) [M] */
    uint16_t laser_state;   /* +0xe8: word-written (930); cmp/zero
                              (931/730/1008) [M] */
    uint16_t heat;          /* +0xea: laser heat, 0xffff max; 930 accumulates,
                              931 compares 0xf000, 938 zeroes [H] */
    uint16_t th[3];         /* +0xec,+0xee,+0xf0: thrust-vector words;
                              zero-init (597); thrust RMW all three (601);
                              1736 dword-cmp@f0 overlaps (suspect) [M] */
    /* 683 word-inits 0xf8 = 8/0x10, 681/740 byte-read it: high half of
       head[1] carries a mode/flag sub-field. */
    int32_t  head[3];       /* +0xf2,+0xf6,+0xfa: 597-writes/601-reads
                              desired-heading handoff; 740/1094 read [M] */
    uint8_t  target;        /* +0xfe: TARGET slot index, 0 = none (597
                              hostile-vs-player; 28-func AI cluster) [H] */
    uint8_t  ai_state;      /* +0xff: AI-table state 0x0-0x24 (50-func map;
                              597 checks ==4) [H] */
    uint8_t  f100;          /* +0x100: spawn-copied flag (672/689/690 copy al;
                              683 writes; 9-func map) [L] */
    uint8_t  sizecl;        /* +0x101: size class 2/1/0: -extent vs 0x1329/
                              0xaa5 (036/1533); AI-read (770) [H] */
    uint16_t w102;          /* +0x102: OR-accumulated (597 targeting), zeroed
                              (770) [M] */
    uint16_t w104;          /* +0x104: OR-accumulated (597), zeroed (770);
                              1736 dword-write overlaps (suspect) [M] */
    uint16_t w106;          /* +0x106: OR-accumulated (597), zeroed (770);
                              (714/735/741/750/751) [M] */
    uint16_t w108;          /* +0x108: word R/W (610/754/756) [M] */
    uint16_t w10a;          /* +0x10a: word R/W (610/754/756). NOT system id:
                              467's ANISO bases a big struct (0x48fc) [M] */
    uint16_t w10c;          /* +0x10c: word (610/754/756) [M] */
    uint16_t w10e;          /* +0x10e: R/W (610/756); ==0x17 check (1007) [L] */
    uint16_t w110;          /* +0x110: turning word (585 movsx) [M] */
    uint16_t w112;          /* +0x112: turning word (585 movsx) [M] */
    uint16_t w114;          /* +0x114: turning word (585 movsx) [M] */
    uint16_t qty;           /* +0x116: count/health + 0x8000 assassin flag; =
                              S+0x08 - DriveTable at init (036/1533) [H] */
    /* 0x118-0x123 as bytes+D+D+D+gap (clean partition); overlapping D-views
       are real: 1736 inits D@118/11c/120(+124), 770 reads D@11a (= link),
       441/1098 seed D@11e = 0x20000000. Order unverified. */
    uint8_t  spawn;         /* +0x118: spawn state: 0xf9 spawn, 0xf5
                              base-spawned, 0xff sent (1098 writes 0x1/0xff) [H] */
    uint8_t  fuel_npc;      /* +0x119: NPC fuel, consumed by 903 [H] */
    int32_t  link;          /* +0x11a: spawn-set; assassin/mission link?;
                              read (770/733/750/786) [L] */
    int32_t  flg11e;        /* +0x11e: 0x20000000 init (441/1098);
                              (410/931/947) [M] */
    uint8_t  _u122[2];      /* +0x122 */
    /* callsign string: 1070 writes single-char (game+0xf4+0x31)+NUL, model
       0xea inherits via strcpy; 278 asterisk-check (ANISO), 297 writes
       first byte; 1736 D@124 + empty-check D@128; 291 reads 124/130/134;
       runs to 0x137 where sc138 begins [H] */
    char     name[20];      /* +0x124-0x137 */
    /* scaled model constants via 1495: value = M+0x2c/M+0x30 words,
       shift = dim-sum-8; flight pushes/ORs halves separately
       (575/819/576); read (441) [H] */
    int64_t  sc138;         /* +0x138: scaled(M+0x2c) */
    int64_t  sc140;         /* +0x140: scaled(M+0x30) */
    int32_t  acc148;        /* +0x148: RMW accumulator, init 0x7f (570);
                              read (1736) [M] */
    uint8_t  flg14c;        /* +0x14c: model flags: low3 = M+0x24>>4&7,
                              bit5 tested by 1070 (setter unknown), bit7 =
                              named-tag nonempty (036/1533). v2's 0x10/0x40
                              here unconfirmed (0x151 confusion?) [H] */
    uint8_t  c14d;          /* +0x14d: countdown: 732 inits 0x13 then decs
                              (cargo AI); 597 subs, 909 zeroes [M] */
    uint8_t  f14e;          /* +0x14e: AI-tested flag (725/727/1025); zeroed
                              (938) [M] */
    uint8_t  b14f;          /* +0x14f: zeroed (938; 937) [M] */
    uint8_t  c150;          /* +0x150: RMW byte (610 only) [L] */
    /* equipment-effect flags: 931 ORs 0x02/0x04/0x08/0x10; 937 sets/
       clears 0x40; 938 ANDs 0x9f ("remove ecm and energy bomb" ANISO:
       bits 0x20+0x40) [H] */
    uint8_t  eq151;         /* +0x151 */
} Actor;
_Static_assert(sizeof(Actor) == 0x152, "slot stride");

static inline Actor *slot_at(int i)  /* 1-based */
    { return (Actor *)(POOL_PTR + 0x74 + 0x152 * i); }

/* Index 0 = null/none (alloc failure returns 0; target 0 = no target) and
 * owns the +0x74..+0x1C5 frame. Live slots are indices 1..114. */
typedef struct __attribute__((packed)) {
    uint8_t _u00;             /* +0x00 */
    uint8_t flags[POOL_N];    /* +0x01: slot i <-> flags[i-1]; value = kind,
                                 bit 0x20 = evictable */
    uint8_t _u73;             /* +0x73 */
    uint8_t null_frame[0x152];/* +0x74..+0x1C5: slot-0 frame */
    Actor   slots[POOL_N];    /* +0x1C6: slot i <-> slots[i-1] */
} Pool;
_Static_assert(sizeof(Pool) == 1 + POOL_N + 1 + 0x152 + POOL_N * 0x152,
               "pool blob");
```

## Scoping warnings (same offset, different struct)

+0xc8 on the game-state struct (`DATA_008804`) is the player-ship pointer,
not equipment bits. Game-state+0xc6 (NOT actor+0xc6) carries ANISO's
illegal-shooting flag from 301. 1054's 0xea dispatch and 0xef arg are
game-state bytes. GetModelPtr results (+0x38 …) are model *static* data.
467's "current system" +0x10a bases a big mission struct (0x48fc), not a
slot — this exact trap killed v2's `system` field. Low offsets in
window/transaction code (+0x0c in 555, …) are other structs. Always identify
the base register before reading a field.

## Open gaps and leads

13 unknown bytes: +0x00 (no literal touches), +0x24 (1736-only), +0x59,
+0x8a, +0x98-0x9b, +0x122-0x123. Widths inferred map-only: 0x0c/0x10/0x14/
0x18/0x1c/0x20 (mat rows), 0x56-0x58 (FOR bytes read as bytes — fine),
w5a-absorbed, w86-slot/87-kind byte-split (sets differ, writers thin).
Overlaps recorded but order/phase unverified: 770's D@11a (= link) vs
d118/D@11c/D@11e views; 441/1098's 0x11e seed vs 1736's D@120; 1736's D@104
vs 597/770's words; 1736's dword-cmp@f0 vs th[2]; 441's D@cc init vs wce;
683's W@f8 vs head[1]. 1736's base: aligns at 0x14-0x20/0x118-0x148 but its
0x24-0x34 reads skew +2 from 1070's verified layout — read 1736 fully to
identify its struct. Leads: 1686/1685/1773/1780 (1683's callees — the true
spawn sink is among them), 1536/1775 (1052's snapshot consumers), flg14c
bit5 setter, 862's full role (pos low-halves + 0x82/0x86/0x9e/0xa4),
game+0xcc slot's role vs +0xc8, model 0xea's identity, 1054's branch
semantics, slot-0x86 stamper, kind-value producers, police models 0x45/0x4F,
0x89's file-wide death. Sibling structs still unmapped: window param
blocks, decoder contexts, callback-queue nodes, the game-state structs
(mission records done — see MISSION_RECORD.md).

## Per-type note (unverified)

The 0x118-0x123 D-views may be one slot with several layouts: a common
header with a tail whose meaning depends on kind/proc (ship vs station vs
missile). Test: split the slot taint on kind values per slot and check
whether reader sets diverge by type.
