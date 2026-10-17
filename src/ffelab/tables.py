"""Find function-pointer tables in ffedat.asm, especially module event tables.

FFE modules expose a 12-slot event dispatch table: slot N holds the module's
handler for event N (0 = reset, 1 = escape, 3 = background, 4 = action,
8 = switch-off, 10 = song update, ...) or a null dword if the module doesn't
handle it. Example (SysMenu):

    DATA_004962:        ; Event function table for SysMenu
        dd FUNC_000218_SysMenuReset_Event0
        dd FUNC_000224_SysMenuProcessEsc_Event1
        db 0x0, 0x0, 0x0, 0x0
        ...

Every table gives confirmed public entry points of one module, which anchor
that module's source file in the address-ordered function list.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

DATA_LABEL_RE = re.compile(r"^(_?DATA_(\d+)\w*):\s*(?:;\s*(.*))?$")
FUNC_RE = re.compile(r"_?FUNC_(\d+)(?:_\w+)?")
EVENT_SLOTS = 12


@dataclass
class Table:
    label: str
    data_num: int | None
    comment: str
    slots: list[int | None]                 # function number per slot, None = null slot
    kind: str = "func_table"                # "event_table" | "func_table"
    notes: list[str] = field(default_factory=list)

    @property
    def functions(self) -> list[int]:
        return [s for s in self.slots if s is not None]


def _slot_items(line: str):
    """Yield per-dword items for one line: ('func', n) | ('zero',) | ('other',)."""
    code, _, _ = line.partition(";")
    code = code.strip()
    if code.startswith("dd "):
        for tok in (t.strip() for t in code[3:].split(",")):
            if (m := FUNC_RE.fullmatch(tok)):
                yield ("func", int(m.group(1)))
            elif re.fullmatch(r"0x0+|0", tok):
                yield ("zero",)
            else:
                yield ("other",)
    elif code.startswith("db "):
        toks = [t.strip() for t in code[3:].split(",")]
        if all(re.fullmatch(r"0x0+|0", t) for t in toks):
            for _ in range(len(toks) // 4):
                yield ("zero",)
            if len(toks) % 4:
                yield ("other",)            # misaligned bytes: not a clean pointer table
        else:
            yield ("other",)
    elif code:
        yield ("other",)


def find_tables(path: str | Path) -> list[Table]:
    tables: list[Table] = []
    lines = Path(path).read_text(errors="replace").splitlines()
    block: list | None = None
    label = comment = ""
    data_num: int | None = None

    def flush():
        if block is None:
            return
        if not block or any(it[0] == "other" for it in block):
            return
        funcs = [it[1] for it in block if it[0] == "func"]
        if len(funcs) < 2:
            return
        slots = [it[1] if it[0] == "func" else None for it in block]
        # trim trailing nulls beyond the event-table width
        t = Table(label=label, data_num=data_num, comment=comment or "", slots=slots)
        core = slots[:EVENT_SLOTS]
        if len(slots) in (EVENT_SLOTS - 1, EVENT_SLOTS) and None in core:
            t.kind = "event_table"
        elif "event" in (comment or "").lower():
            t.kind = "event_table"
            t.notes.append(f"labelled event table but has {len(slots)} slots")
        tables.append(t)

    for line in lines:
        m = DATA_LABEL_RE.match(line.strip())
        if m:
            flush()
            label, comment = m.group(1), (m.group(3) or "").strip()
            data_num = int(m.group(2))
            block = []
            continue
        if block is None:
            continue
        if line.strip().upper().startswith(("GLOBAL", "EXTERN", "SECTION")):
            flush()
            block = None
            continue
        block.extend(_slot_items(line))
    flush()
    return tables
