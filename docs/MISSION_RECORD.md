# Mission-record layout (static research notes)

Missions are 0x34-byte POD records in a flat array at `DATA_009085`, count in
`DATA_009084`. Stride triple-confirmed (300's memcpy compaction, 316's and
1125's `+0x34` walks). Four functions read records (300 awards, 316 display,
722 military-target, 1125 photo-camera); 276/296 test emptiness, 280 writes
the count (purpose open), 300 decrements on completion. Field confidence:
[H]igh / [M]edium / [L]ow. Zero gaps — every byte attributed.
All fields are values (ids, dates, credits, indices) — this struct holds
no pointers, so no u32pointer appears below.

```c
#include <stdint.h>

#define MREC_N     (*(int *)DATA_009084)
#define MREC_BASE  ((uint8_t *)DATA_009085)

typedef struct __attribute__((packed)) Mission {
    uint32_t id;            /* +0x00: index into special-ships table 008548
                                (034 setter / 035_GetSpecialShips getter;
                                316 passes it) [H] */
    uint8_t  type;           /* +0x04: lo nibble = kind (3/4 photo-family per
                                1125; 8 special per 316); hi nibble = tier:
                                award bands 60/100/215 (300) [H] */
    uint8_t  done;           /* +0x05: 0 active; 0xff set by 722+1125 on
                                completion [H] */
    uint32_t sys_a;          /* +0x06: system id (1125/722 match vs current
                                system 008885) [M] */
    uint32_t due_lo;         /* +0x0a: deadline vs game date 008804 [M] */
    uint32_t due_hi;         /* +0x0e: deadline vs 008807 (64-bit date) [M] */
    uint32_t reward;         /* +0x12: x10 = credits paid (300) [M+] */
    uint32_t model;          /* +0x16: ship model for display (GetModelPtr) [M] */
    uint8_t  target;         /* +0x1a: target object id; !=0 required;
                                resolved via [007754] (722+1125) [M] */
    uint8_t  rank;           /* +0x1b: <=8 full award else 3/4 (300);
                                min-accumulated (1125) [M] */
    char     text[20];       /* +0x1c: display string, lea-passed (316) [M] */
    uint32_t sys_b;          /* +0x30: system id (300 matches vs 008885) [M] */
} Mission;
_Static_assert(sizeof(Mission) == 0x34, "record stride");

static inline Mission *mrec_at(int i) { return (Mission *)(MREC_BASE + 0x34*i); }
```

## Open questions

- sys_a vs sys_b: origin/destination pair, or per-type union? Completion (300)
  matches sys_b; photo/military checks (1125/722) match sys_a. Delivery,
  assassination, and photo branches in 316 display different halves.
- Type enum only partly decoded (3/4/8 observed in branches; full list open).
- 280 writes the mission count during milbase init — allocate, reserve, reset?
- Sibling array: `DATA_009094` (count `DATA_009104`) is a SECOND 0x34-strided
  array managed by 248 and walked by spawn funcs 243/246. Identity open
  (spawn queue? manifests?). 248 compacts it with the same rep-movsd idiom.
