"""Match FFE functions to Frontier: Elite II (68k) through glFrontier's ``fe2.s``.

``fe2.s`` is Tom Morton's labelled disassembly of the Atari ST ``frontier.prg``
(shipped by glFrontier forks, not by pcercuei/glfrontier, and not by us: it is a
disassembly of a copyrighted game, so it is read from a path you supply).

FFE re-implements much of FE2 with the same constants and tables but different
instructions, so matching uses what survives a rewrite (docs/REFERENCES.md):

1. data tables found in ``ffedat.asm`` after an endian swap (or word -> dword);
   a table used by exactly one function on each side pairs those functions;
2. distinctive immediate constants (TF-IDF cosine, mutual best, enough weight);
3. propagation over the call graph from those anchors, BinDiff-style.

Results go to ``ref_matches``: reference facts with a confidence, never ledger
evidence. They never settle a bet or raise a contradiction, and must stay out
of ``blind_view`` (they carry other people's names).
"""
from __future__ import annotations

import bisect
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .asm import Function, parse_asm

REF = "glfrontier"
# Starting confidences by how a pair was found. From the first run's hold-out
# (86% precision re-finding hidden anchors) and the name check (5 of 8 agree),
# shaded down for weak propagated pairs.
CONFIDENCE = {"data": 0.8, "const": 0.8, "propagated": 0.7, "propagated_weak": 0.45}
WEAK_CSIM = 0.3
ANCHOR_STRENGTH = 8.0      # summed IDF of shared constants for a constant anchor
MIN_SCORE, MIN_MARGIN = 0.25, 0.05

# ---------------------------------------------------------------- fe2.s parser
_TOP = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")
_LOCAL = re.compile(r"^\s+([a-z][A-Za-z0-9_]*):")
_EQU = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s+equ\s+(\S+)")
_IMM = re.compile(r"#(-?\$?[0-9a-fA-F]+|-?\d+)\b")
_DC = re.compile(r"^\s*(?:l[0-9a-f]+:)?\s*dc\.([bwl])\s+(.*?)\s*(?:;.*)?$")


def _num(s: str) -> int:
    s = s.strip(); neg = s.startswith("-"); s = s.lstrip("-")
    v = int(s[1:], 16) if s.startswith("$") else int(s, 0)
    return -v if neg else v


@dataclass
class Block:
    """One top-level label's worth of fe2.s: a function, or a data block."""
    label: str
    lines: list[str] = field(default_factory=list)
    imms: list[int] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    data: bytearray | None = field(default_factory=bytearray)   # big-endian bytes; None once code is seen

    @property
    def addr(self) -> int | None:
        m = re.match(r"^[LN]([0-9a-f]+)", self.label)
        return int(m.group(1), 16) if m else None

    @property
    def name(self) -> str | None:
        """Tom Morton's name, if the label carries one (``L368e0_Sqrt`` -> ``Sqrt``)."""
        m = re.match(r"^[LN][0-9a-f]+_(.+)$", self.label)
        if m:
            return m.group(1)
        return None if re.match(r"^[LN][0-9a-f]+$", self.label) else self.label

    @property
    def is_code(self) -> bool:
        return self.data is None and len(self.lines) >= 3


def _dc_bytes(width: int, operands: str) -> bytes:
    out = bytearray()
    for t in re.findall(r'"[^"]*"|\'[^\']*\'|[^,]+', operands):
        t = t.strip()
        if not t:
            continue
        if t[0] in "\"'":
            out += t[1:-1].encode("latin-1"); continue
        try:
            v = _num(t)
        except ValueError:
            out += b"\xAA" * width; continue                   # a label or expression: placeholder
        out += (v & ((1 << (8 * width)) - 1)).to_bytes(width, "big")
    return bytes(out)


def parse_fe2(text: str) -> list[Block]:
    equ: dict[str, int] = {}
    blocks: list[Block] = []
    cur: Block | None = None
    for line in text.splitlines():
        m = _EQU.match(line)
        if m:
            try:
                equ[m.group(1)] = _num(m.group(2))
            except ValueError:
                pass
            continue
        m = _TOP.match(line)
        if m:
            cur = Block(m.group(1)); blocks.append(cur); line = line[m.end():]
        if cur is None:
            continue
        md = _DC.match(line)
        if md:
            if cur.data is not None:
                cur.data += _dc_bytes({"b": 1, "w": 2, "l": 4}[md.group(1)], md.group(2))
            continue
        body = line.split(";")[0]
        if re.match(r"^\s*\*", body):
            continue
        body = _LOCAL.sub(" ", re.sub(r"^\s*:", " ", body)).strip()
        if not body:
            continue
        if cur.data is not None and len(cur.data) == 0:
            cur.data = None                                        # first statement is code
        if cur.data is not None:
            continue
        cur.lines.append(body)
        for v in _IMM.findall(body):
            try:
                cur.imms.append(_num(v))
            except ValueError:
                pass
        mc = re.match(r"^(jsr|bsr(?:\.[bsw])?|jmp)\s+(\S+)", body)
        if mc:
            tgt = mc.group(2)
            m5 = re.match(r"^(-?\w+)\(a5\)$", tgt)
            if m5:                                                  # through the a5 jump table
                t = m5.group(1)
                off = int(t) if re.match(r"^-?\d+$", t) else equ.get(t)
                if off is not None:
                    cur.calls.append(f"@a5:{off}")
            elif re.match(r"^[A-Za-z_]\w*$", tgt) and not re.match(r"^l[0-9a-f]+$", tgt):
                cur.calls.append(tgt)
    by = {b.label: b for b in blocks}
    a5: dict[int, str] = {}
    tab = next((b for b in blocks if b.label.endswith("_a5_jumptab")), None)
    if tab:
        for i, ins in enumerate(tab.lines):
            mj = re.match(r"^jmp\s+(\S+)", ins)
            if not mj:
                break
            a5[6 * i] = mj.group(1)
    for b in blocks:
        out = []
        for c in b.calls:
            c = a5.get(int(c[4:]), "") if c.startswith("@a5:") else c
            if c in by and c != b.label:
                out.append(c)
        b.calls = out
    return blocks


# ---------------------------------------------------------------- ffedat.asm
def parse_ffedat(text: str) -> tuple[bytes, list[tuple[int, int]]]:
    """Little-endian data blob and (offset, DATA number) for each label."""
    blob = bytearray(); starts: list[tuple[int, int]] = []
    for line in text.splitlines():
        m = re.match(r"^DATA_(\d+):", line)
        if m:
            starts.append((len(blob), int(m.group(1)))); line = line[m.end():]
        m = re.match(r"^\s*(db|dw|dd)\s+(.*?)\s*(?:;.*)?$", line)
        if m:
            w = {"db": 1, "dw": 2, "dd": 4}[m.group(1)]
            out = bytearray()
            for t in re.findall(r'"[^"]*"|\'[^\']*\'|[^,]+', m.group(2)):
                t = t.strip()
                if not t:
                    continue
                if t[0] in "\"'":
                    out += t[1:-1].encode("latin-1"); continue
                try:
                    v = int(t, 0)
                except ValueError:
                    out += b"\xAA" * w; continue
                out += (v & ((1 << (8 * w)) - 1)).to_bytes(w, "little")
            blob += out
            continue
        m = re.match(r"^\s*resb\s+(\S+)", line)
        if m:
            try:
                blob += b"\0" * int(m.group(1), 0)
            except ValueError:
                pass
    return bytes(blob), starts


def _transform(b: bytes, how: str) -> bytes:
    if how == "b":
        return b
    if how in ("w", "l"):
        w = 2 if how == "w" else 4
        return b"".join(b[i:i + w][::-1] for i in range(0, len(b) - len(b) % w, w))
    out = bytearray()                                              # "w2l"/"w2ls": 68k word -> x86 dword
    for i in range(0, len(b) - 1, 2):
        out += (int.from_bytes(b[i:i + 2], "big", signed=how == "w2ls") & 0xffffffff).to_bytes(4, "little")
    return bytes(out)


def _interesting(b: bytes) -> bool:
    return len(set(b)) >= 6 and b.count(0) < len(b) * 0.7


def match_tables(blocks: list[Block], blob: bytes, starts: list[tuple[int, int]],
                 min_len: int = 24, min_frac: float = 0.5) -> dict[str, int]:
    """fe2 data label -> FFE DATA number, for tables whose content reappears in ffedat."""
    offs = [s for s, _ in starts]
    out = {}
    for b in blocks:
        if b.data is None or len(b.data) < min_len or not _interesting(bytes(b.data[:64])):
            continue
        best = None
        for how in ("w", "l", "b", "w2l", "w2ls"):
            s = _transform(bytes(b.data), how)
            for start in range(0, max(0, min(len(s) - 16, 256)), 16):
                probe = s[start:start + 16]
                if not _interesting(probe):
                    continue
                i = blob.find(probe)
                if i < 0 or i - start < 0:
                    continue
                j = i - start
                n = sum(1 for k in range(min(len(s), len(blob) - j)) if s[k] == blob[j + k])
                if best is None or n / len(s) > best[1]:
                    best = (j, n / len(s))
                break
        if best and best[1] > min_frac:
            k = bisect.bisect_right(offs, best[0]) - 1
            if k >= 0:
                out[b.label] = starts[k][1]
    return out


# ---------------------------------------------------------------- constants
def _canon(v: int, word: bool = False) -> int:
    v &= 0xffffffff
    if v >= 0x80000000:
        v -= 1 << 32
    if 0x8000 <= v <= 0xffff and word:
        v -= 0x10000
    return v


def _keep(v: int) -> bool:
    return abs(v) >= 0x40 and abs(v) not in (0x7f, 0x80, 0xff, 0x100, 0xfff, 0x1000, 0x7fff, 0x8000, 0xffff)


def fe2_constants(b: Block) -> Counter:
    return Counter(k for k in (_canon(v, word=True) for v in b.imms) if _keep(k))


def ffe_constants(f: Function) -> Counter:
    c = Counter()
    for ins in f.instructions:
        parts = ins.split(";")[0].split(None, 1)
        if len(parts) < 2 or parts[0].startswith(("j", "call", "loop")):
            continue
        ops = re.sub(r"\[[^\]]*\]", "", parts[1])
        for tok in re.findall(r"(?:^|,)\s*(?:byte |word |dword )?\+?(-?(?:0x[0-9a-fA-F]+|\d+))\s*(?=,|$)", ops):
            try:
                v = int(tok, 0)
            except ValueError:
                continue
            k = _canon(v, word=True)      # 68k word ops often became 32-bit ops: fold both sides alike
            if _keep(k):
                c[k] += 1
    return c


# ---------------------------------------------------------------- matching
class Matcher:
    def __init__(self, ffe: list[Function], fe2: list[Block], tables: dict[str, int]):
        self.ffe = {f.num: f for f in ffe}
        self.fe2 = {b.label: b for b in fe2 if b.is_code}
        A = {n: ffe_constants(f) for n, f in self.ffe.items()}
        B = {k: fe2_constants(b) for k, b in self.fe2.items()}
        df = Counter()
        for c in list(A.values()) + list(B.values()):
            df.update(c.keys())
        N = len(A) + len(B)
        self.idf = {k: math.log(N / d) for k, d in df.items()}

        def vec(c):
            v = {k: (1 + math.log(n)) * self.idf[k] for k, n in c.items()}
            nrm = math.sqrt(sum(x * x for x in v.values())) or 1
            return {k: x / nrm for k, x in v.items()}
        self.VA = {k: vec(c) for k, c in A.items() if c}
        self.VB = {k: vec(c) for k, c in B.items() if c}
        self.fsucc = {n: (set(f.calls) | set(f.tail_jumps)) & set(self.ffe) for n, f in self.ffe.items()}
        self.gsucc = {k: set(b.calls) & set(self.fe2) for k, b in self.fe2.items()}
        self.fpred, self.gpred = defaultdict(set), defaultdict(set)
        for a, s in self.fsucc.items():
            for c in s:
                self.fpred[c].add(a)
        for b, s in self.gsucc.items():
            for c in s:
                self.gpred[c].add(b)
        self.tables = tables

    def csim(self, a: int, b: str) -> float:
        va, vb = self.VA.get(a), self.VB.get(b)
        return sum(x * vb.get(k, 0.0) for k, x in va.items()) if va and vb else 0.0

    def strength(self, a: int, b: str) -> float:
        return sum(self.idf[k] for k in set(self.VA.get(a, ())) & set(self.VB.get(b, ())))

    def const_anchors(self, min_strength: float = ANCHOR_STRENGTH) -> dict[int, str]:
        inv = defaultdict(list)
        for b, v in self.VB.items():
            for k, x in v.items():
                inv[k].append((b, x))
        best = {}
        for a, va in self.VA.items():
            s = Counter()
            for k, x in va.items():
                for b, y in inv.get(k, ()):
                    s[b] += x * y
            if s:
                b, sc = s.most_common(1)[0]
                best[a] = (b, sc)
        rbest = {}
        for a, (b, sc) in best.items():
            if b not in rbest or sc > rbest[b][1]:
                rbest[b] = (a, sc)
        return {a: b for a, (b, _) in best.items()
                if rbest[b][0] == a and self.strength(a, b) >= min_strength}

    def data_anchors(self) -> dict[int, str]:
        g = defaultdict(set)
        for b in self.fe2.values():
            for ins in b.lines:
                for tok in re.findall(r"\b([LN][0-9a-f]+\w*)", ins):
                    if tok in self.tables:
                        g[self.tables[tok]].add(b.label)
        f = defaultdict(set)
        for n, fn in self.ffe.items():
            for d in fn.data_refs:
                f[d].add(n)
        return {next(iter(f[d])): next(iter(gs)) for d, gs in g.items()
                if len(gs) == 1 and len(f.get(d, ())) == 1}

    def _score(self, a: int, b: str) -> float:
        fa, gb = len(self.ffe[a].instructions), len(self.fe2[b].lines)
        s = self.csim(a, b) + max(0.0, 0.3 - 0.15 * abs(math.log((fa + 3) / (gb + 3))))
        na, nb = len(self.fsucc[a]), len(self.gsucc[b])
        return s + 0.1 * (min(na, nb) / (max(na, nb) or 1))

    def propagate(self, seed: dict[int, str], rounds: int = 10) -> dict[int, str]:
        M = dict(seed); R = {b: a for a, b in M.items()}
        for _ in range(rounds):
            cand: dict[tuple[int, str], float] = {}
            for a, b in list(M.items()):
                for na, nb in ((self.fsucc[a], self.gsucc[b]), (self.fpred[a], self.gpred[b])):
                    xs = [x for x in na if x not in M]; ys = [y for y in nb if y not in R]
                    for x in xs:
                        for y in ys:
                            cand[(x, y)] = max(cand.get((x, y), 0.0), self._score(x, y))
            bya, byb = defaultdict(list), defaultdict(list)
            for (x, y), s in cand.items():
                bya[x].append((s, y)); byb[y].append((s, x))
            new = 0
            for x, opts in bya.items():
                opts.sort(reverse=True); s, y = opts[0]
                if s < MIN_SCORE or (len(opts) > 1 and s - opts[1][0] < MIN_MARGIN):
                    continue
                back = sorted(byb[y], reverse=True)
                if back[0][1] != x or (len(back) > 1 and back[0][0] - back[1][0] < MIN_MARGIN):
                    continue
                if x in M or y in R:
                    continue
                M[x] = y; R[y] = x; new += 1
            if not new:
                break
        return M

    def seed(self) -> tuple[dict[int, str], dict[int, str], dict[int, str]]:
        consts, data = self.const_anchors(), self.data_anchors()
        seed = dict(consts)
        for a, b in data.items():
            if a not in seed and b not in seed.values():
                seed[a] = b
        return seed, consts, data

    def holdout(self, frac: float = 0.3, repeats: int = 5, rng_seed: int = 1) -> dict:
        """Hide ``frac`` of the anchors, propagate from the rest, and count how many hidden
        ones come back and how many come back right. Precision is against other static
        anchors, not experiments: an upper bound on how far to trust propagated pairs."""
        import random
        seed = self.seed()[0]
        rng = random.Random(rng_seed); keys = sorted(seed); hidden = found = right = 0
        for _ in range(repeats):
            rng.shuffle(keys); hid = set(keys[: int(len(keys) * frac)])
            M = self.propagate({a: seed[a] for a in keys if a not in hid})
            for a in hid:
                hidden += 1
                if a in M:
                    found += 1; right += M[a] == seed[a]
        return {"hidden": hidden, "recall": round(found / max(hidden, 1), 3),
                "precision": round(right / max(found, 1), 3)}

    def run(self) -> list[dict]:
        seed, consts, data = self.seed()
        rows = []
        for a, b in sorted(self.propagate(seed).items()):
            cs = self.csim(a, b)
            how = ("data" if data.get(a) == b else "const" if consts.get(a) == b
                   else "propagated" if cs >= WEAK_CSIM else "propagated_weak")
            blk = self.fe2[b]
            rows.append({"func": a, "ref": REF, "ref_label": b, "ref_name": blk.name, "ref_addr": blk.addr,
                         "how": how, "csim": round(cs, 3), "confidence": CONFIDENCE[how]})
        return rows


def matcher(fe2_path: Path, src: Path) -> Matcher:
    """A matcher for ``src/ffe.asm`` (+ ``src/ffedat.asm``) against an fe2.s file."""
    blocks = parse_fe2(Path(fe2_path).read_text(encoding="latin-1"))
    blob, starts = parse_ffedat(Path(src, "ffedat.asm").read_text(encoding="latin-1"))
    tables = match_tables(blocks, blob, starts)
    return Matcher(parse_asm(Path(src, "ffe.asm")), blocks, tables)


def store(con, rows: list[dict], ref: str = REF) -> int:
    """Replace this reference's matches (a re-run supersedes the last one)."""
    con.execute("DELETE FROM ref_matches WHERE ref=?", (ref,))
    con.executemany(
        "INSERT INTO ref_matches (func, ref, ref_label, ref_name, ref_addr, how, csim, confidence)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [(r["func"], r["ref"], r["ref_label"], r["ref_name"], r["ref_addr"], r["how"], r["csim"],
          r["confidence"]) for r in rows])
    con.commit()
    return len(rows)


def matches_for(con, func: int) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT ref, ref_label, ref_name, ref_addr, how, csim, confidence FROM ref_matches"
        " WHERE func=? ORDER BY confidence DESC", (func,))]
