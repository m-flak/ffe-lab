"""Emulator bring-up helpers: find ``code_base`` and save named QEMU states.

``calibrate`` asks the running game where its code is: it takes a few
distinctive byte runs from your FIRSTENC.EXE, searches guest memory for them
over the GDB stub, and checks that framed functions really start with
``push ebp; mov ebp,esp`` at ``code_base + Pos``. It detaches at the end, so
the game keeps running.

``save_state`` runs ``savevm NAME`` on the QEMU monitor and waits for it to
finish; ``snapshots`` lists the states QEMU has. See docs/EMULATOR.md.
"""
from __future__ import annotations

import socket
import sqlite3
from pathlib import Path

from .. import exe
from .gdb import GdbRemote

PROMPT = b"(qemu) "
PROBE_LEN = 32
CHUNK = 1024                    # bytes per GDB memory read; QEMU caps replies near 2 KB


# ---------------------------------------------------------------- QEMU monitor
def monitor(cmd: str, port: int = 4444, timeout: float = 120.0) -> str:
    """Run one monitor command and return its output once QEMU prints the next prompt."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        _read_until_prompt(s)
        s.sendall(cmd.encode() + b"\n")
        out = _read_until_prompt(s).decode(errors="replace")
    lines = [ln for ln in out.replace("\r", "").split("\n") if ln.strip() and ln.strip() != cmd]
    return "\n".join(ln for ln in lines if not ln.startswith("(qemu)")).strip()


def _read_until_prompt(s: socket.socket) -> bytes:
    buf = b""
    while not buf.endswith(PROMPT):
        chunk = s.recv(4096)
        if not chunk:
            raise ConnectionError("QEMU monitor closed the connection")
        buf += chunk
    return buf


def save_state(name: str, port: int = 4444) -> str:
    out = monitor(f"savevm {name}", port)
    if "rror" in out:
        raise RuntimeError(f"savevm {name} failed: {out}")
    return out


def snapshots(port: int = 4444) -> set[str]:
    """Snapshot tags QEMU knows (``info snapshots``); empty if the monitor isn't reachable."""
    try:
        out = monitor("info snapshots", port, timeout=10)
    except OSError:
        return set()
    names = set()
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) >= 2 and parts[0].isdigit():           # "ID  TAG  VM SIZE  DATE ..."
            names.add(parts[1])
        elif len(parts) >= 2 and parts[0] == "--":           # newer QEMU prints "--" for the ID
            names.add(parts[1])
    return names


# ---------------------------------------------------------------- code_base
def _framed(con: sqlite3.Connection, binary: str | None) -> list[tuple[int, int]]:
    """(func, Pos) of framed functions, in this binary's address space when mapped."""
    framed = {r["num"] for r in con.execute("SELECT num FROM functions WHERE has_frame=1")}
    if binary:
        pos = exe.load_map(con, binary, min_conf=0.7)
    else:
        pos = {r["num"]: r["pos"] for r in con.execute("SELECT num,pos FROM functions WHERE pos IS NOT NULL")}
    return sorted((f, p) for f, p in pos.items() if f in framed)


def pick_probes(data: bytes, framed: list[tuple[int, int]], n: int = 3) -> list[tuple[int, bytes]]:
    """Byte runs at function starts that occur exactly once in the exe's code, spread over the code."""
    code = data[exe.FCODEOFF:exe.FCODEOFF + exe.FCODESIZE + 0x20000]
    out: list[tuple[int, bytes]] = []
    step = max(1, len(framed) // (n + 1))
    for i in range(step, len(framed), step):
        pos = framed[i][1]
        off = exe.to_offset(pos)
        probe = data[off:off + PROBE_LEN]
        if len(probe) == PROBE_LEN and probe[:3] in exe.PROLOGUES and code.count(probe) == 1:
            out.append((pos, probe))
        if len(out) == n:
            break
    return out


def search(gdb: GdbRemote, probe: bytes, start: int, end: int, progress=None) -> int | None:
    """Linear address of ``probe`` in guest memory [start, end), or None."""
    tail = b""
    addr = start & ~(CHUNK - 1)
    while addr < end:
        try:
            block = gdb.read_mem(addr, CHUNK)
        except IOError:                                     # unmapped (paging): skip the block
            tail, addr = b"", addr + CHUNK
            continue
        buf = tail + block
        i = buf.find(probe)
        if i >= 0:
            return addr - len(tail) + i
        tail = buf[-(len(probe) - 1):]
        addr += CHUNK
        if progress and addr % (1 << 22) == 0:
            progress(addr)
    return None


def calibrate(con: sqlite3.Connection, exe_path: str | Path, binary: str | None = None,
              host: str = "127.0.0.1", port: int = 1234, limit_mb: int = 64,
              progress=None) -> dict:
    data = Path(exe_path).read_bytes()
    framed = _framed(con, binary)
    probes = pick_probes(data, framed)
    if not probes:
        raise RuntimeError("no unique function-start bytes found; is this FIRSTENC.EXE, and did you run exe-check?")
    gdb = GdbRemote(host, port)
    try:
        regs = gdb.registers()                               # QEMU stops the guest when GDB attaches
        eip = regs["eip"]
        # The game is usually running when we stop it, so look near EIP first.
        regions = [(max(0, eip - 0x100000), eip + 0x100000), (0, limit_mb << 20)]
        bases = []
        for pos, probe in probes:
            hit = None
            for lo, hi in regions:
                hit = search(gdb, probe, lo, hi, progress)
                if hit is not None:
                    break
            bases.append(None if hit is None else hit - pos)
        found = [b for b in bases if b is not None]
        if not found:
            return {"ok": False, "eip": eip, "reason": "the code isn't in guest memory: is FFE running "
                    "(in flight or docked, not at the DOS prompt)? Try a larger --limit-mb."}
        base = max(set(found), key=found.count)
        sample = framed[::max(1, len(framed) // 60)]
        good = 0
        for _, pos in sample:
            try:
                good += gdb.read_mem(base + pos, 3) in exe.PROLOGUES
            except IOError:
                pass
        lo, hi = base + exe.VCODEOFF, base + exe.VCODEOFF + exe.FCODESIZE
        return {
            "ok": found.count(base) == len(probes) and good >= 0.9 * len(sample),
            "code_base": base, "code_base_hex": hex(base),
            "probes_agree": f"{found.count(base)}/{len(probes)}",
            "prologues_ok": f"{good}/{len(sample)}",
            "eip": hex(eip),
            "eip_in_code": lo <= eip < hi,
        }
    finally:
        try:
            gdb.command("D")                                 # detach: QEMU resumes the guest
        except OSError:
            pass
        finally:
            gdb.close()
