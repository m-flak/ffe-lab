"""Parse the JJFFE/FFED3D NASM disassembly (ffe.asm) into structured functions.

Each function in ffe.asm looks like:

    FUNC_000036:            ; Pos = 12528
    _FUNC_000035_GetSpecialShips:   ; Pos = 12518

The number is the disassembler's sequence id, the optional suffix is a name
added by later hackers, and ``Pos`` is the function's offset (hex) in the
original executable. Sequence ids follow address order almost perfectly, which
is what lets us treat the list as the original link layout.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

LABEL_RE = re.compile(r"^(_?FUNC_(\d+)(?:_(\w+))?):\s*(?:;\s*Pos\s*=\s*([0-9a-fA-F]+))?")
FUNC_REF_RE = re.compile(r"\b_?FUNC_(\d+)(?:_\w+)?")
DATA_REF_RE = re.compile(r"\b_?DATA_(\d+)")
CALL_DIRECT_RE = re.compile(r"^call\s+(?:near\s+)?_?FUNC_(\d+)")
CALL_INDIRECT_RE = re.compile(r"^call\s+(?:near\s+)?\[\s*_?DATA_(\d+)\s*\]")
JMP_FUNC_RE = re.compile(r"^jmp\s+(?:near\s+|short\s+)?_?FUNC_(\d+)")


@dataclass
class Function:
    num: int
    pos: int | None = None
    name: str | None = None
    labels: list[str] = field(default_factory=list)
    instructions: list[str] = field(default_factory=list)
    calls: list[int] = field(default_factory=list)          # direct + resolved indirect
    indirect_calls: list[int] = field(default_factory=list)  # DATA slots called through
    tail_jumps: list[int] = field(default_factory=list)
    data_refs: list[int] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"FUNC_{self.num:06d}" + (f"_{self.name}" if self.name else "")

    @property
    def has_frame(self) -> bool:
        return self.instructions[:2] == ["push ebp", "mov ebp,esp"]

    @property
    def filler_tail(self) -> int:
        """Count trailing nop/int3 filler instructions (padding before next function)."""
        n = 0
        for ins in reversed(self.instructions):
            if ins in ("nop", "int3"):
                n += 1
            else:
                break
        return n

    @property
    def aligned4(self) -> bool | None:
        return None if self.pos is None else self.pos % 4 == 0

    def prefix(self) -> str | None:
        """First CamelCase word of the hacker-assigned name, e.g. 'SysMenu' -> 'Sys'.

        Uses known multi-word prefixes first so SysMenu / CdrFile / FF30 stay whole.
        """
        if not self.name:
            return None
        return name_prefix(self.name)


KNOWN_PREFIXES = (
    "SysMenu", "CdrFile", "Console", "StringExpand", "StringConvert", "StringDraw",
    "String", "FF30", "FF80", "Int64", "Vec64", "Mat", "DPal", "Keyb", "Gui", "Timer",
    "Mouse", "ScrnShot", "Scrnshot", "Sound", "Video", "Draw", "Blit", "Tex", "Text",
    "Spawn", "Get", "Sys", "File",
)


def name_prefix(name: str) -> str:
    for p in KNOWN_PREFIXES:
        if name.startswith(p):
            return p
    m = re.match(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", name)
    return m.group(0) if m else name


def _strip(line: str) -> tuple[str, str]:
    code, _, comment = line.partition(";")
    return code.strip(), comment.strip()


def parse_asm(path: str | Path) -> list[Function]:
    """Parse ffe.asm into Functions in file order."""
    funcs: list[Function] = []
    cur: Function | None = None
    for raw in Path(path).read_text(errors="replace").splitlines():
        m = LABEL_RE.match(raw)
        if m:
            num, name, pos = int(m.group(2)), m.group(3), m.group(4)
            if cur is not None and cur.num == num:      # alias label for same function
                cur.labels.append(m.group(1))
                if name:
                    cur.name = name
                if pos and cur.pos is None:
                    cur.pos = int(pos, 16)
                continue
            cur = Function(num=num, name=name, pos=int(pos, 16) if pos else None,
                           labels=[m.group(1)])
            funcs.append(cur)
            continue
        if cur is None:
            continue
        code, comment = _strip(raw)
        if not code or code.endswith(":") or code.upper().startswith(("GLOBAL", "EXTERN", "SECTION", "BITS")):
            continue
        cur.instructions.append(code)
        if (c := CALL_DIRECT_RE.match(code)):
            cur.calls.append(int(c.group(1)))
        elif (c := CALL_INDIRECT_RE.match(code)):
            cur.indirect_calls.append(int(c.group(1)))
            # disassembler annotates resolved targets: "call near [DATA_007756] ; FUNC_001538_GetModelPtr"
            if (t := FUNC_REF_RE.search(comment)):
                cur.calls.append(int(t.group(1)))
        elif (j := JMP_FUNC_RE.match(code)):
            cur.tail_jumps.append(int(j.group(1)))
        for d in DATA_REF_RE.findall(code):
            cur.data_refs.append(int(d))
    return funcs


def address_ordered(funcs: list[Function]) -> list[Function]:
    """Functions with a known Pos, sorted by original address."""
    return sorted((f for f in funcs if f.pos is not None), key=lambda f: f.pos)
