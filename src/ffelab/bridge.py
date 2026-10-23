"""The Zig side: let a researcher read the new sokol-zig engine next to FFE and
record how the two should connect. Protocol: ``docs/STATIC_RESEARCH.md``
("Frankensteining: the Zig engine").

Three parts:

- ``ZigRepo``: read-only access to the engine's source, confined to one root
  directory (``FFELAB_ZIG_ROOT``). List, read, grep and outline; nothing writes.
- ``subsystem``: FFE seen as a box to cut out: its members, the entry points the
  rest of the game calls, what it calls outside itself, and the data it shares.
  Every member carries its belief, so a shaky boundary looks shaky.
- ``cut_check``: before gutting or modifying a function for the new UI, what it
  reaches (presentation vs game logic), which data it reads and *writes*, and
  whether it looks safe to gut, needs splitting, or should stay.
- links (the ``bridges`` table): how an FFE function, data label, segment or
  label should meet the Zig code ("Zig ``gfx.drawTri`` replaces FUNC_001616").
  These are design proposals, not findings about FFE: they never touch beliefs,
  bets or evidence, and the Zig code is never a basis for a claim.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from fnmatch import fnmatch
from pathlib import Path

from .ledger import Ledger
from .research import DATA_LABEL_RE, FUNC_LABEL_RE, _name

SKIP_DIRS = {".git", ".zig-cache", "zig-cache", "zig-out", "node_modules"}
TEXT_EXT = {".zig", ".zon", ".md", ".txt", ".glsl", ".h", ".c", ".json", ".toml"}
MAX_FILE = 1 << 20                     # don't grep or read files over 1 MB (generated blobs)

RELATIONS = ("replaces", "wraps", "stubs", "calls_into", "reads", "mirrors")
STATUSES = ("proposed", "accepted", "in_progress", "done", "rejected")

DECL_RES = [
    ("fn", re.compile(r"^\s*(pub\s+)?(?:(?:export|extern|inline|noinline)\s+(?:\"\w+\"\s+)?)*fn\s+(\w+)")),
    ("type", re.compile(r"^\s*(pub\s+)?const\s+(\w+)\s*(?::[^=]+)?=\s*(?:extern\s+|packed\s+)?"
                        r"(?:struct|enum|union|opaque)\b")),
    ("const", re.compile(r"^(pub\s+)?(?:const|var)\s+(\w+)\b")),        # top level only
    ("test", re.compile(r"^\s*()test\s+\"([^\"]*)\"")),
]


# ---------------------------------------------------------------- the Zig repo
class ZigRepo:
    """Read-only view of one directory tree. Paths are relative to the root; anything
    that resolves outside it (``..``, absolute paths, symlinks out) is refused."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"Zig root {self.root} is not a directory")

    def _inside(self, p: Path) -> bool:
        return p == self.root or self.root in p.parents

    def path(self, rel: str = "") -> Path:
        p = (self.root / rel).resolve()
        if not self._inside(p):
            raise ValueError(f"{rel!r} is outside the Zig root")
        return p

    def _files(self, sub: str = "", glob: str = "*"):
        base = self.path(sub)
        paths = [base] if base.is_file() else sorted(base.rglob("*"))
        for p in paths:
            rel = p.relative_to(self.root)
            if (p.is_file() and not SKIP_DIRS & set(rel.parts) and p.suffix in TEXT_EXT
                    and fnmatch(p.name, glob) and self._inside(p.resolve())):
                yield p, rel

    def tree(self, sub: str = "", glob: str = "*") -> list[dict]:
        """Source files under ``sub`` with their sizes in lines."""
        return [{"path": str(rel), "lines": _count_lines(p)} for p, rel in self._files(sub, glob)]

    def read(self, rel: str, start: int = 1, end: int | None = None, max_lines: int = 400) -> dict:
        """Lines ``start``..``end`` (1-based, inclusive), numbered, at most ``max_lines``."""
        p = self.path(rel)
        if not p.is_file():
            raise KeyError(f"no file {rel}")
        if p.stat().st_size > MAX_FILE:
            raise ValueError(f"{rel} is over 1 MB; read a range with grep first")
        lines = p.read_text(errors="replace").splitlines()
        start = max(start, 1)
        end = min(end or len(lines), len(lines), start + max_lines - 1)
        body = "\n".join(f"{n:5d}  {lines[n - 1]}" for n in range(start, end + 1))
        return {"path": rel, "lines": len(lines), "start": start, "end": end, "text": body}

    def grep(self, pattern: str, glob: str = "*.zig", limit: int = 50) -> list[dict]:
        """Lines matching a regex, as ``{"path", "line", "text"}``."""
        rx = re.compile(pattern)
        out = []
        for p, rel in self._files("", glob):
            if p.stat().st_size > MAX_FILE:
                continue
            for n, ln in enumerate(p.read_text(errors="replace").splitlines(), 1):
                if rx.search(ln):
                    out.append({"path": str(rel), "line": n, "text": ln.strip()[:200]})
                    if len(out) >= limit:
                        return out
        return out

    def outline(self, rel: str) -> list[dict]:
        """Declarations in one file: functions, types, top-level consts and tests, with lines.
        Regex-based, so it can miss unusual formatting; read the file when it matters."""
        p = self.path(rel)
        if not p.is_file():
            raise KeyError(f"no file {rel}")
        out = []
        for n, ln in enumerate(p.read_text(errors="replace").splitlines(), 1):
            for kind, rx in DECL_RES:
                m = rx.match(ln)
                if m:
                    out.append({"kind": kind, "name": m.group(2), "pub": bool(m.group(1)), "line": n})
                    break
        return out


def _count_lines(p: Path) -> int | None:
    if p.stat().st_size > MAX_FILE:
        return None
    with p.open("rb") as f:
        return sum(1 for _ in f)


# ---------------------------------------------------------------- FFE as a box
def members(ledger: Ledger, spec: str, min_prob: float = 0.5) -> dict[int, float | None]:
    """Functions in a subsystem, with their belief in it.

    ``spec`` is a segment (``seg:12``) or a label path (``render`` or ``render/planet``).
    A label takes functions whose top belief is its top level at ``min_prob`` or more;
    a deeper path also needs a label at or under that path (``func_labels``). A segment
    takes its whole FUNC range, and the value is the segment's own label belief: None.
    """
    con = ledger.con
    if spec.startswith("seg:"):
        s = con.execute("SELECT first_ord, last_ord FROM segments WHERE seg=?", (int(spec[4:]),)).fetchone()
        if s is None:
            raise KeyError(f"no segment {spec}")
        return {r["num"]: None for r in con.execute(
            "SELECT num FROM functions WHERE ord BETWEEN ? AND ? ORDER BY num", (s["first_ord"], s["last_ord"]))}
    top = spec.split("/")[0]
    if top not in ledger.labels:
        raise ValueError(f"{spec!r}: not a segment (seg:N) or a label under {', '.join(ledger.labels)}")
    best: dict[int, tuple[str, float]] = {}
    for r in con.execute("SELECT func, label, prob FROM beliefs"):
        if r["func"] not in best or r["prob"] > best[r["func"]][1]:
            best[r["func"]] = (r["label"], r["prob"])
    out = {f: p for f, (lab, p) in best.items() if lab == top and p >= min_prob}
    if "/" in spec:
        deep = {r["func"] for r in con.execute(
            "SELECT DISTINCT func FROM func_labels WHERE path=? OR path LIKE ?", (spec, spec + "/%"))}
        out = {f: p for f, p in out.items() if f in deep}
    return dict(sorted(out.items()))


def subsystem(ledger: Ledger, spec: str, min_prob: float = 0.5, limit: int = 40) -> dict:
    """A subsystem as a box to cut out of FFE: who calls in, what it calls out to,
    which data it shares with the rest of the game and which only it touches."""
    con = ledger.con
    mem = members(ledger, spec, min_prob)
    inside = set(mem)
    callers_in: dict[int, set[int]] = defaultdict(set)        # member -> outside callers
    calls_out: dict[int, set[int]] = defaultdict(set)         # outside callee -> members calling it
    for r in con.execute("SELECT DISTINCT caller, callee FROM calls"):
        a, b = r["caller"], r["callee"]
        if b in inside and a not in inside:
            callers_in[b].add(a)
        elif a in inside and b not in inside:
            calls_out[b].add(a)
    touch: dict[int, set[int]] = defaultdict(set)              # data -> funcs touching it
    for r in con.execute("SELECT DISTINCT func, data FROM data_refs"):
        touch[r["data"]].add(r["func"])
    ours = {d: fs for d, fs in touch.items() if fs & inside}
    shared = sorted(((d, len(fs & inside), len(fs - inside)) for d, fs in ours.items() if fs - inside),
                    key=lambda t: -t[2])
    private = sorted(d for d, fs in ours.items() if not fs - inside)

    def lab(f):
        b = ledger.belief(f)
        top = max(b, key=b.get)
        return f"{top}@{b[top]:.2f}"

    weak = [f for f, p in mem.items() if p is not None and p < 0.7]
    return {
        "subsystem": spec, "members": len(mem), "min_prob": min_prob if not spec.startswith("seg:") else None,
        "weak_members": [_name(con, f) for f in weak][:limit],
        "member_list": [{"func": _name(con, f), "belief": None if p is None else round(p, 2)}
                        for f, p in mem.items()][:limit],
        "entry_points": [{"func": _name(con, f), "outside_callers": len(cs),
                          "e.g.": [_name(con, c) for c in sorted(cs)[:3]]}
                         for f, cs in sorted(callers_in.items(), key=lambda kv: -len(kv[1]))][:limit],
        "calls_out": [{"func": _name(con, f), "label": lab(f), "from_members": len(ms)}
                      for f, ms in sorted(calls_out.items(), key=lambda kv: -len(kv[1]))][:limit],
        "shared_data": [{"data": f"DATA_{d:06d}", "members": m, "outside_funcs": o} for d, m, o in shared][:limit],
        "private_data": [f"DATA_{d:06d}" for d in private][:limit],
        "note": "membership follows current beliefs (not settled unless experiments say so); "
                "data sharing is from code references, so pointer-passed data is missed",
    }


# ---------------------------------------------------------------- cut check
# Mnemonics whose first operand is written (two-operand forms) or whose only operand is.
WRITES_DEST = {"mov", "add", "sub", "and", "or", "xor", "adc", "sbb", "shl", "shr", "sal", "sar",
               "rol", "ror", "rcl", "rcr", "xchg", "xadd", "cmpxchg", "bts", "btr", "btc",
               "not", "neg", "inc", "dec", "pop", "fst", "fstp", "fist", "fistp", "fnstsw", "fnstcw"}
DATA_IN_RE = re.compile(r"\b_?DATA_(\d+)")

PRESENTATION = {"render", "ui", "audio", "platform", "text"}   # what the new engine redraws or replaces
LOGIC = {"physics", "ai", "mission", "economy", "sysgen"}       # what has to survive a cut
# math, io and unknown count as neither: shared helpers or not yet known
PRESENT_NAMES = re.compile(r"^(Draw|Blit|Bmp|Text|Persist|Video|Gui|Console|DPal|Sound|Keyb|Mouse|Ptr|"
                           r"ScrnShot|Clear|Enable|Disable|Update|SetPtr|String)")
PRESENT_EXTERNS = re.compile(r"^_(C_|Video|Input|Timer|Sound|Dir|BlitClip|FillAvi|ResetAvi|RadarCargo)")
EXTERN_CALL_RE = re.compile(r"^(?:call|jmp)\s+(?:near\s+)?(_(?!FUNC_|DATA_)[A-Za-z]\w*)")


def _operands(ins: str) -> tuple[str, list[str]]:
    ins = ins.split(";")[0].strip()
    if not ins:
        return "", []
    parts = ins.split(None, 1)
    ops, depth, cur = [], 0, ""
    for ch in parts[1] if len(parts) > 1 else "":
        if ch == "," and depth == 0:
            ops.append(cur.strip()); cur = ""
            continue
        depth += ch == "["; depth -= ch == "]"
        cur += ch
    if cur.strip():
        ops.append(cur.strip())
    return parts[0].lower(), ops


def data_access(asm_text: str) -> dict[int, str]:
    """Per data label a function mentions: ``read``, ``write`` (a store into it, which may
    also read), or ``addr`` (its address is taken, so a callee may write through it).
    Indexed stores (``[edx*4+DATA_x]``) count as writes to the table at ``DATA_x``."""
    out: dict[int, str] = {}
    rank = {"read": 0, "addr": 1, "write": 2}

    def mark(d: int, how: str):
        if d not in out or rank[how] > rank[out[d]]:
            out[d] = how

    for ln in (asm_text or "").splitlines():
        op, ops = _operands(ln)
        for i, o in enumerate(ops):
            for m in DATA_IN_RE.finditer(o):
                d = int(m.group(1))
                if "[" not in o:                                   # bare label: an address, not a load
                    mark(d, "addr")
                elif i == 0 and op in WRITES_DEST:
                    mark(d, "write")
                else:
                    mark(d, "read")
        if op == "lea":
            for m in DATA_IN_RE.finditer(" ".join(ops[1:])):
                mark(int(m.group(1)), "addr")
    return out


def _access_index(ledger: Ledger) -> dict[int, dict[int, str]]:
    """func -> {data: read|write|addr}, parsed once per ledger."""
    idx = getattr(ledger, "_bridge_access", None)
    if idx is None:
        idx = {r["num"]: data_access(r["asm_text"]) for r in
               ledger.con.execute("SELECT num, asm_text FROM functions")}
        ledger._bridge_access = idx
    return idx


def side(ledger: Ledger, func: int, min_prob: float = 0.5) -> tuple[str, str]:
    """``presentation`` | ``logic`` | ``neutral`` | ``unknown``, and why."""
    r = ledger.con.execute("SELECT name FROM functions WHERE num=?", (func,)).fetchone()
    name = (r["name"] if r else None) or ""
    b = ledger.belief(func)
    top = max(b, key=b.get)
    if b[top] >= min_prob:
        how = "presentation" if top in PRESENTATION else "logic" if top in LOGIC else "neutral"
        return how, f"belief {top}@{b[top]:.2f}"
    if name and PRESENT_NAMES.match(name):
        return "presentation", f"name {name}"
    return "unknown", f"no belief over {min_prob} (top {top}@{b[top]:.2f})"


def cut_check(ledger: Ledger, func: int, min_prob: float = 0.5) -> dict:
    """What gutting or modifying ``func`` would cost.

    Verdicts (a static prediction, settled only by a knockout on the emulator):
    ``gut`` presentation only: it draws, calls nothing on the logic side, and writes
    nothing logic code reads. ``split`` it draws *and* touches game logic or game state:
    move the logic out before gutting the drawing. ``keep`` game logic with no drawing.
    ``unsure`` too much it reaches has no label yet; the reasons say what."""
    con = ledger.con
    row = con.execute("SELECT num, asm_text FROM functions WHERE num=?", (func,)).fetchone()
    if row is None:
        raise KeyError(f"no FUNC {func}")
    idx = _access_index(ledger)
    callees = [r["callee"] for r in con.execute(
        "SELECT DISTINCT callee FROM calls WHERE caller=? ORDER BY callee", (func,))]
    externs = sorted({m.group(1) for ln in (row["asm_text"] or "").splitlines()
                      if (m := EXTERN_CALL_RE.match(ln.strip()))})
    sides = {c: side(ledger, c, min_prob) for c in callees}
    own = side(ledger, func, min_prob)

    readers: dict[int, set[int]] = defaultdict(set)
    for f, acc in idx.items():
        for d, how in acc.items():
            if how in ("read", "write"):
                readers[d].add(f)
    access = idx.get(func, {})
    writes = []
    for d, how in sorted(access.items()):
        if how == "read":
            continue
        others = readers[d] - {func}
        logic_readers = [o for o in others if side(ledger, o, min_prob)[0] == "logic"]
        writes.append({"data": f"DATA_{d:06d}", "how": how, "other_readers": len(others),
                       "logic_readers": [_name(con, o) for o in sorted(logic_readers)[:5]]})

    draws = (own[0] == "presentation" or any(s == "presentation" for s, _ in sides.values())
             or any(PRESENT_EXTERNS.match(e) for e in externs))
    logic_calls = [c for c, (s, _) in sides.items() if s == "logic"]
    state_writes = [w for w in writes if w["logic_readers"]]
    unknown = [c for c, (s, _) in sides.items() if s == "unknown"]
    reasons = []
    if logic_calls:
        reasons.append(f"calls game logic: {', '.join(_name(con, c) for c in logic_calls[:5])}")
    if state_writes:
        reasons.append(f"writes data that logic code reads: {', '.join(w['data'] for w in state_writes[:5])}")
    if unknown:
        reasons.append(f"{len(unknown)} of {len(callees)} callees have no label yet")
    if any(w["how"] == "addr" for w in writes):
        reasons.append("passes data addresses on, so callees may write through them")
    touches_logic = bool(logic_calls or state_writes or own[0] == "logic")
    if draws and touches_logic:
        verdict = "split"
    elif touches_logic:
        verdict = "keep"
    elif draws and not unknown:
        verdict = "gut"
    else:
        verdict = "unsure"
    return {
        "func": _name(con, func), "verdict": verdict, "reasons": reasons,
        "own_side": {"side": own[0], "why": own[1]},
        "callers": [_name(con, r["caller"]) for r in con.execute(
            "SELECT DISTINCT caller FROM calls WHERE callee=? ORDER BY caller", (func,))],
        "in_tables": [{"table": t["label"], "comment": t["comment"], "slot": i}
                      for t in con.execute("SELECT label, comment, slots FROM tables")
                      for i, f in enumerate(json.loads(t["slots"] or "[]")) if f == func],
        "callees": [{"func": _name(con, c), "side": s, "why": why} for c, (s, why) in sides.items()],
        "externs": externs,
        "data_reads": [f"DATA_{d:06d}" for d, how in sorted(access.items()) if how == "read"],
        "data_writes": writes,
        "note": "static prediction from beliefs and asm; one level of callees; stores through "
                "registers (e.g. [esi+0x10]) aren't attributed to a label. A knockout settles it.",
    }


# ---------------------------------------------------------------- links
def _check_ffe(ledger: Ledger, ref: str) -> str:
    ref = ref.strip()
    if m := FUNC_LABEL_RE.match(ref):
        num = int(m.group(1))
        if ledger.con.execute("SELECT 1 FROM functions WHERE num=?", (num,)).fetchone() is None:
            raise KeyError(f"no FUNC {num}")
        return f"FUNC_{num:06d}"
    if m := DATA_LABEL_RE.match(ref):
        return f"DATA_{int(m.group(1)):06d}"
    if ref.startswith("seg:"):
        members(ledger, ref)                                  # raises if there's no such segment
        return ref
    if ref.split("/")[0] in ledger.labels:
        return ref
    raise ValueError(f"{ref!r}: give FUNC_n, DATA_n, seg:N or a label path")


def link(ledger: Ledger, ffe: str, zig: str, relation: str, note: str, source: str,
         repo: ZigRepo | None = None) -> dict:
    """Propose how an FFE piece meets the Zig code. ``zig`` is ``path`` or ``path:symbol``
    (or a planned name not written yet). A proposal only: status starts at ``proposed``
    and only the human moves it (``ffelab bridge status``)."""
    if relation not in RELATIONS:
        raise ValueError(f"relation must be one of {', '.join(RELATIONS)}")
    if not note.strip():
        raise ValueError("a link needs a note: why these two meet, and what has to change")
    ffe = _check_ffe(ledger, ffe)
    exists = None
    if repo is not None:
        exists = repo.path(zig.split(":")[0]).is_file()       # refuses paths outside the root
    rid = ledger.con.execute(
        "INSERT INTO bridges (ffe, zig, relation, status, note, source) VALUES (?,?,?,?,?,?) RETURNING id",
        (ffe, zig.strip(), relation, "proposed", note.strip(), source)).fetchone()["id"]
    ledger.con.commit()
    return {"id": rid, "ffe": ffe, "zig": zig.strip(), "relation": relation, "status": "proposed",
            "zig_file_exists": exists}


def links(con, ffe: str | None = None, zig: str | None = None, status: str | None = None) -> list[dict]:
    """Links, filtered by an FFE ref (exact), a Zig path prefix, or a status."""
    q, args = "SELECT * FROM bridges WHERE 1=1", []
    if ffe:
        q += " AND ffe=?"; args.append(ffe)
    if zig:
        q += " AND zig LIKE ?"; args.append(zig + "%")
    if status:
        q += " AND status=?"; args.append(status)
    return [dict(r) for r in con.execute(q + " ORDER BY id", args)]


def set_status(con, link_id: int, status: str) -> dict:
    """The human's call on a proposal."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    if con.execute("UPDATE bridges SET status=? WHERE id=?", (status, link_id)).rowcount == 0:
        raise KeyError(f"no link {link_id}")
    con.commit()
    return dict(con.execute("SELECT * FROM bridges WHERE id=?", (link_id,)).fetchone())
