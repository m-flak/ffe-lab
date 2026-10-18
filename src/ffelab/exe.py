"""Check an FFE executable against the disassembly, and map addresses if it differs.

John Jordan's disassembler (ffed3d doc/srctools/ffedis.cpp) read a FIRSTENC.EXE
with a fixed layout: code at file offset 0xE00, size 0x69200, loaded at virtual
address 0x10000. Every ``Pos`` in ffe.asm is a virtual address in that image:

    file offset = Pos - 0x10000 + 0xE00

``check`` tells you in seconds whether your exe is that build: 94% of FFE
functions open with ``push ebp; mov ebp,esp`` (55 8B EC or 55 89 E5), so we
look for those bytes where the asm says each function starts.

If it's a different build, ``align`` maps functions by content. Builds of the
same game keep the same files in the same link order, so the shift between
them is piecewise constant: a changed function moves everything after it by
the same amount. For each function we find the shift that the most of its
neighbours agree on and accept it if the exe really has a prologue there.
"""
from __future__ import annotations

import bisect
import hashlib
import sqlite3
from collections import Counter
from pathlib import Path

VCODEOFF = 0x10000
FCODEOFF = 0xE00
FCODESIZE = 0x69200
MIN_FILE_SIZE = 0x1F6800 + 0x9A00     # end of relocation table in the disassembled exe
PROLOGUES = (b"\x55\x8b\xec", b"\x55\x89\xe5")


def to_offset(pos: int) -> int:
    return pos - VCODEOFF + FCODEOFF


def to_pos(offset: int) -> int:
    return offset - FCODEOFF + VCODEOFF


def binary_id(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _funcs(con: sqlite3.Connection) -> list[tuple[int, int, bool]]:
    return [(r["num"], r["pos"], bool(r["has_frame"])) for r in
            con.execute("SELECT num, pos, has_frame FROM functions WHERE pos IS NOT NULL ORDER BY pos")]


def has_prologue(data: bytes, off: int) -> bool:
    return 0 <= off and data[off:off + 3] in PROLOGUES


# ---------------------------------------------------------------- check
def check(data: bytes, funcs: list[tuple[int, int, bool]]) -> dict:
    framed = [(n, p) for n, p, f in funcs if f]
    hits = [has_prologue(data, to_offset(p)) for _, p in framed]
    rate = sum(hits) / len(hits) if hits else 0.0
    # match rate per tenth of the code, to show where builds diverge
    deciles = []
    for k in range(10):
        part = hits[k * len(hits) // 10:(k + 1) * len(hits) // 10]
        lo = framed[k * len(framed) // 10][1]
        deciles.append({"from_pos": hex(lo), "match": round(sum(part) / len(part), 3) if part else None})
    # first point after which matching collapses (rolling window of 20)
    diverge = None
    w = 20
    for i in range(len(hits) - w):
        if sum(hits[i:i + w]) / w < 0.5 and rate < 0.95:
            j = next(k for k in range(i, i + w) if not hits[k])   # first miss inside the collapse
            diverge = {"func": framed[j][0], "pos": hex(framed[j][1])}
            break
    if rate >= 0.95:
        verdict = "same_build"
    elif rate >= 0.2 or (deciles[0]["match"] or 0) >= 0.8:
        verdict = "related_build"
    else:
        verdict = "different_layout"
    return {
        "size": len(data), "size_ok": len(data) >= MIN_FILE_SIZE, "mz": data[:2] == b"MZ",
        "checked": len(hits), "matched": sum(hits), "match_rate": round(rate, 4),
        "verdict": verdict, "deciles": deciles, "diverges_at": diverge,
    }


# ---------------------------------------------------------------- align
def scan_prologues(data: bytes, start: int = FCODEOFF, end: int | None = None) -> list[int]:
    end = min(end or FCODEOFF + FCODESIZE + 0x20000, len(data))
    found = []
    for pat in PROLOGUES:
        i = data.find(pat, start, end)
        while i != -1:
            found.append(i)
            i = data.find(pat, i + 1, end)
    return sorted(found)


def align(data: bytes, funcs: list[tuple[int, int, bool]], radius: int = 0x8000,
          window: int = 4) -> list[dict]:
    """Map each function to an offset in ``data``. Returns rows with
    func, pos (asm), offset, mapped_pos, confidence, method."""
    starts = scan_prologues(data)
    framed = [(n, p) for n, p, f in funcs if f]
    exp = [to_offset(p) for _, p in framed]
    # candidate shifts per framed function
    cand: list[set[int]] = []
    for e in exp:
        lo = bisect.bisect_left(starts, e - radius)
        hi = bisect.bisect_right(starts, e + radius)
        cand.append({s - e for s in starts[lo:hi]})
    rows: dict[int, dict] = {}
    prev_shift = 0
    for i, (num, pos) in enumerate(framed):
        votes = Counter()
        span = range(max(0, i - window), min(len(framed), i + window + 1))
        for k in span:
            votes.update(cand[k])
        if not votes:
            continue
        # most votes; ties -> continuity with the previous shift, then the smallest shift
        shift = min(votes, key=lambda s: (-votes[s], abs(s - prev_shift), abs(s)))
        support = votes[shift] / len(span)
        if shift in cand[i]:
            prev_shift = shift
            rows[num] = {"func": num, "pos": pos, "offset": exp[i] + shift, "shift": shift,
                         "confidence": round(support, 3), "method": "prologue_vote"}
    # functions without a prologue (or unmatched): interpolate the shift from neighbours
    by_pos = sorted(funcs, key=lambda t: t[1])
    mapped = [(p, rows[n]["shift"]) for n, p, _ in by_pos if n in rows]
    mp = [p for p, _ in mapped]
    for num, pos, framed_flag in by_pos:
        if num in rows or not mapped:
            continue
        i = bisect.bisect_left(mp, pos)
        left = mapped[i - 1] if i > 0 else None
        right = mapped[i] if i < len(mapped) else None
        if left and right and left[1] == right[1]:
            shift, conf = left[1], 0.7
        else:
            shift, conf = (left or right)[1], 0.3
        rows[num] = {"func": num, "pos": pos, "offset": to_offset(pos) + shift, "shift": shift,
                     "confidence": conf if not framed_flag else conf * 0.5,
                     "method": "interpolated" if not framed_flag else "changed?"}
    out = sorted(rows.values(), key=lambda r: r["pos"])
    for r in out:
        r["mapped_pos"] = to_pos(r["offset"])
    return out


# ---------------------------------------------------------------- store / load
def store(con: sqlite3.Connection, bin_id: str, path: str, report: dict, rows: list[dict]) -> None:
    con.execute("INSERT INTO binaries (id, path, size, verdict, match_rate) VALUES (?,?,?,?,?)"
                " ON CONFLICT (id) DO UPDATE SET path=excluded.path, size=excluded.size,"
                " verdict=excluded.verdict, match_rate=excluded.match_rate",
                (bin_id, path, report["size"], report["verdict"], report["match_rate"]))
    con.execute('DELETE FROM addr_maps WHERE "binary"=?', (bin_id,))   # quoted: a Postgres keyword
    con.executemany('INSERT INTO addr_maps ("binary", func, pos, confidence, method) VALUES (?,?,?,?,?)',
                    [(bin_id, r["func"], r["mapped_pos"], r["confidence"], r["method"]) for r in rows])
    con.commit()


def load_map(con: sqlite3.Connection, bin_id: str, min_conf: float = 0.0) -> dict[int, int]:
    return {r["func"]: r["pos"] for r in con.execute(
        'SELECT func, pos FROM addr_maps WHERE "binary"=? AND confidence>=?', (bin_id, min_conf))}


def run(con: sqlite3.Connection, exe_path: str | Path, force_map: bool = False) -> dict:
    data = Path(exe_path).read_bytes()
    funcs = _funcs(con)
    report = check(data, funcs)
    bid = binary_id(data)
    if report["verdict"] == "same_build" and not force_map:
        rows = [{"func": n, "mapped_pos": p, "confidence": 1.0, "method": "identity"} for n, p, _ in funcs]
    else:
        rows = align(data, funcs)
        report["mapped"] = len(rows)
        report["mapped_confident"] = sum(r["confidence"] >= 0.7 for r in rows)
        report["suspected_changed"] = [r["func"] for r in rows if r["method"] == "changed?"][:50]
    store(con, bid, str(exe_path), report, rows)
    report["binary_id"] = bid
    return report
