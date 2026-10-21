"""Drive a real emulator: window input/screenshots on macOS + GDB-protocol debugging.

Two supported setups:

* **QEMU + FreeDOS** (most scriptable): ``qemu-system-i386 ... -gdb tcp::1234
  -monitor tcp:127.0.0.1:4444,server,nowait``. Breakpoints over GDB, save
  states via the monitor (``savevm`` / ``loadvm``), input via the monitor's
  ``sendkey``. No window automation needed.
* **DOSBox-X** (best compatibility): input via macOS System Events, screenshots
  via ``screencapture``, save states via DOSBox-X's own hotkeys (set in
  config), debugging via a GDB stub if your build has one.

Beyond the screen (QEMU): memory watchpoints over the GDB stub; DOS file
calls caught with plain breakpoints on the game's own ``int 21h``/``int 31h``
sites (DOS/4GW runs the game flat, so DS:EDX is a linear address); and
sound-card port writes counted from QEMU's ``memory_region_ops_write`` trace
event, switched on from the monitor around each run (no change to
``run-ffe.sh``, so save states stay valid). The GDB protocol has no I/O-port
breakpoints, so the trace says *that* the sound changed, not who wrote it.

UNTESTED against a live emulator so far: the protocol pieces are unit-tested
against a fake stub; expect to adjust key maps and timings once.
"""
from __future__ import annotations

import bisect
import os
import re
import socket
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

from ..halt import HaltSnapshot
from ..models import FileAccess, Watch, WatchHit
from .backend import AddressMap, GameBackend
from .gdb import GdbRemote, parse_stop

# macOS virtual key codes for keys FFE uses
MAC_KEYCODES = {
    "F1": 122, "F2": 120, "F3": 99, "F4": 118, "F5": 96, "F6": 97, "F7": 98, "F8": 100,
    "F9": 101, "F10": 109, "F11": 103, "F12": 111, "Esc": 53, "Enter": 36, "Space": 49,
    "Tab": 48, "Left": 123, "Right": 124, "Down": 125, "Up": 126,
}
# QEMU device (memory region) names whose port writes count as sound
AUDIO_DEVICES = ("sb16", "adlib", "pcspk")
TRACE_EVENT = "memory_region_ops_write"
_TRACE_RE = re.compile(r"addr 0x([0-9a-fA-F]+) value 0x[0-9a-fA-F]+ size \d+ name '([^']*)'")

# DOS int 21h functions that touch files; which register holds the filename
DOS_FILE_FNS = {0x3C: "edx", 0x3D: "edx", 0x3E: None, 0x3F: None, 0x40: None, 0x41: "edx",
                0x42: None, 0x43: "edx", 0x4E: "edx", 0x4F: None, 0x56: "edx", 0x5B: "edx", 0x6C: "esi"}
DOS_HANDLE_FNS = {0x3E, 0x3F, 0x40, 0x42}

# QEMU monitor sendkey names
QEMU_KEYS = {k: k.lower() for k in MAC_KEYCODES} | {"Esc": "esc", "Enter": "ret", "Space": "spc"}


@dataclass
class EmulatorConfig:
    kind: str = "qemu"                       # "qemu" | "dosbox-x"
    gdb_host: str = "127.0.0.1"
    gdb_port: int = 1234
    monitor_port: int = 4444                 # qemu only
    window_app: str = "DOSBox-X"             # dosbox-x only: app to focus
    save_state_keys: dict[str, list[str]] = field(default_factory=dict)  # dosbox-x slot hotkeys
    load_state_keys: dict[str, list[str]] = field(default_factory=dict)
    code_base: int = 0                       # runtime address of Pos 0 (calibrate once)
    audio_trace: bool = True                 # qemu only: count sound-card port writes in knockouts
    trace_dir: str = ""                      # where QEMU writes the trace log ("" = system temp)


class EmulatorBackend(GameBackend):
    def __init__(self, cfg: EmulatorConfig, func_positions: dict[int, int],
                 func_sizes: dict[int, int] | None = None):
        self.cfg = cfg
        self.amap = AddressMap(cfg.code_base)
        self.gdb = GdbRemote(cfg.gdb_host, cfg.gdb_port)
        self.func_pos = func_positions                       # func num -> Pos
        self._sorted = sorted((p, f) for f, p in func_positions.items())
        self._starts = [p for p, _ in self._sorted]
        # upper bound on each function's length (MAX_INSN_BYTES x instructions); without
        # sizes only the code below the first function and far past the last is unmapped
        self.func_size = func_sizes or {}
        self.watches: list[Watch] = []
        self._trace_path: str | None = None
        self._trace_ok: bool | None = None                    # None: not tried yet
        self.breaks: dict[int, int] = {}                     # runtime addr -> func
        self.counts: dict[int, int] = {}
        self.log_only = False
        self.originals: dict[int, bytes] = {}             # func -> bytes replaced by knock_out
        self.last_stop = ""

    # ------------------------------------------------------------ helpers
    def func_at(self, addr: int) -> int | None:
        """Function containing runtime address ``addr``: the nearest start below, if
        ``addr`` is within that function's possible length. None for unmapped code
        (the C runtime, sound drivers, DOS/4GW, DOS), which the asm doesn't cover."""
        pos = self.amap.to_pos(addr)
        i = bisect.bisect_right(self._starts, pos) - 1
        if i < 0:
            return None
        start, f = self._sorted[i]
        size = self.func_size.get(f, 0x4000 if i == len(self._sorted) - 1 else None)
        if size is not None and pos >= start + size:
            return None
        return f

    def _monitor(self, cmd: str) -> str:
        with socket.create_connection(("127.0.0.1", self.cfg.monitor_port), timeout=5) as s:
            s.recv(4096)
            s.sendall(cmd.encode() + b"\n")
            time.sleep(0.3)
            return s.recv(65536).decode(errors="replace")

    def _mac_key(self, key: str) -> None:
        app = self.cfg.window_app
        if key in MAC_KEYCODES:
            action = f"key code {MAC_KEYCODES[key]}"
        elif "+" in key:                                      # e.g. "Alt+F5"
            *mods, k = key.split("+")
            using = ", ".join(f"{m.lower()} down" for m in mods)
            action = (f"key code {MAC_KEYCODES[k]} using {{{using}}}" if k in MAC_KEYCODES
                      else f'keystroke "{k.lower()}" using {{{using}}}')
        else:
            action = f'keystroke "{key}"'
        subprocess.run(["osascript", "-e", f'tell application "{app}" to activate',
                        "-e", f'tell application "System Events" to {action}'], check=True)

    # ------------------------------------------------------------ GameBackend
    def load_state(self, name):
        if self.cfg.kind == "qemu":
            self._monitor(f"loadvm {name}")
        else:
            self.press_keys(self.cfg.load_state_keys[name])

    def save_state(self, name):
        if self.cfg.kind == "qemu":
            self._monitor(f"savevm {name}")
        else:
            self.press_keys(self.cfg.save_state_keys[name])

    def press_keys(self, keys, hold_ms=80, gap_ms=120):
        for k in keys:
            if self.cfg.kind == "qemu":
                self._monitor(f"sendkey {QEMU_KEYS.get(k, k.lower())} {hold_ms}")
            else:
                self._mac_key(k)
            time.sleep(gap_ms / 1000)

    def wait(self, ms):
        """Let the game run for ``ms`` (the GDB stub keeps it stopped otherwise)."""
        reply = self.gdb.cont(timeout=ms / 1000)
        if reply:                                  # stopped early: breakpoint or fault
            self.last_stop = reply
        else:
            self.gdb.interrupt()
            self.last_stop = "interrupted"

    def knock_out(self, func, patch=b"\xc3"):
        addr = self.amap.to_runtime(self.func_pos[func])
        self.originals.setdefault(func, self.gdb.read_mem(addr, len(patch)))
        self.gdb.write_mem(addr, patch)

    def restore(self, func):
        if func in self.originals:
            self.gdb.write_mem(self.amap.to_runtime(self.func_pos[func]), self.originals.pop(func))

    def health(self):
        """Crashed if the last run stopped on a signal other than SIGTRAP (05).
        Frozen if two screenshots a second apart are byte-identical (heuristic:
        a genuinely static scene will also read as frozen, so prefer animated scenes)."""
        stop = getattr(self, "last_stop", "")
        if stop and stop[0] in "TS" and stop[1:3] not in ("05", ""):
            return "crashed"
        a, b = "/tmp/_ffelab_h1", "/tmp/_ffelab_h2"
        self.screenshot(a); self.wait(1000); self.screenshot(b)
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return "frozen" if fa.read() == fb.read() else "ok"

    def screenshot(self, path):
        if self.cfg.kind == "qemu":
            path = os.path.abspath(path)              # QEMU resolves relative paths from its own cwd
            self._monitor(f"screendump {path}")       # PPM; convert if you need PNG
        else:
            subprocess.run(["screencapture", "-x", path], check=True)
        return path

    def set_breakpoints(self, funcs, log_only=False):
        self.log_only = log_only
        for f in funcs:
            addr = self.amap.to_runtime(self.func_pos[f])
            if self.gdb.set_breakpoint(addr):
                self.breaks[addr] = f

    def clear_breakpoints(self):
        for addr in list(self.breaks):
            self.gdb.remove_breakpoint(addr)
        self.breaks.clear(); self.counts.clear()

    def _stop_eip(self) -> int:
        return self.gdb.registers()["eip"]

    def _step_over_break(self, addr: int) -> None:
        self.gdb.remove_breakpoint(addr)
        self.gdb.command("s")
        self.gdb.set_breakpoint(addr)

    def count_hits(self, seconds: float) -> dict[int, int]:
        """Logging pass: count hits for ``seconds`` without stopping for analysis."""
        end = time.time() + seconds
        while time.time() < end:
            reply = self.gdb.cont(timeout=max(0.05, end - time.time()))
            if not reply:
                self.gdb.interrupt()
                break
            addr = self._stop_eip()
            f = self.breaks.get(addr)
            if f is not None:
                self.counts[f] = self.counts.get(f, 0) + 1
                self._step_over_break(addr)
        return dict(self.counts)

    def hit_counts(self):
        return dict(self.counts)

    def stack(self, regs: dict[str, int], depth: int = 8) -> list[int]:
        """Walk the EBP chain (94% of FFE functions keep frames) to caller functions."""
        out, ebp = [], regs["ebp"]
        for _ in range(depth):
            try:
                saved_ebp, ret = struct.unpack("<II", self.gdb.read_mem(ebp, 8))
            except IOError:
                break
            f = self.func_at(ret)
            if f is None:
                break
            out.append(f)
            if saved_ebp <= ebp:
                break
            ebp = saved_ebp
        return out

    def _looks_like_return(self, ret: int) -> bool:
        """Is the instruction just before ``ret`` a call (E8 rel32, or FF /2 in its
        2-, 3- and 6-byte forms)?"""
        try:
            b = self.gdb.read_mem(ret - 6, 6)
        except IOError:
            return False
        return (b[1] == 0xE8 or (b[0] == 0xFF and (b[1] >> 3) & 7 == 2)
                or (b[3] == 0xFF and (b[4] >> 3) & 7 == 2) or (b[4] == 0xFF and (b[5] >> 3) & 7 == 2))

    def scan_stack(self, esp: int, words: int = 256) -> list[int]:
        """Mapped callers found by scanning raw stack words for return addresses.

        For stops in unmapped code (no frame chain to follow): a word counts if it
        points into a mapped function right after a call instruction. Heuristic:
        stale words can match, so innermost-first order matters more than the tail."""
        raw = b""
        for n in (words, 32):                           # near the stack's top a long read can fail
            try:
                raw = self.gdb.read_mem(esp, 4 * n)
                break
            except IOError:
                continue
        out: list[int] = []
        for (v,) in struct.iter_unpack("<I", raw[: len(raw) // 4 * 4]):
            f = self.func_at(v)
            if f is not None and (not out or out[-1] != f) and self._looks_like_return(v):
                out.append(f)
                if len(out) >= 8:
                    break
        return out

    def attribute(self, regs: dict[str, int]) -> tuple[int | None, str | None, list[int]]:
        """(func, how, stack) for a stop at ``regs``: the function at EIP if mapped,
        else the nearest mapped caller on the stack."""
        f = self.func_at(regs["eip"])
        if f is not None:
            return f, "pc", self.stack(regs)
        stack = self.scan_stack(regs["esp"])
        return (stack[0], "stack", stack) if stack else (None, None, [])

    def run_until_halt(self, timeout_ms, scenario=""):
        reply = self.gdb.cont(timeout=timeout_ms / 1000)
        if not reply:
            self.gdb.interrupt()
            return None
        self.last_stop = reply
        regs = self.gdb.registers()
        if (f := self.breaks.get(regs["eip"])) is not None:
            how, stack = "pc", self.stack(regs)
        else:
            f, how, stack = self.attribute(regs)
        esp = regs["esp"]
        try:
            args = list(struct.unpack("<4I", self.gdb.read_mem(esp + 4, 16)))
        except IOError:
            args = []
        return HaltSnapshot(func=f, scenario=scenario, hits=self.counts.get(f, 1),
                            stack=stack, regs=regs, args=args, pc=regs["eip"], attributed_by=how)

    def resume(self):
        self.gdb.cont(timeout=0.01)

    def read_mem(self, addr, length):
        return self.gdb.read_mem(addr, length)

    # ------------------------------------------------------------ watchpoints
    def set_watchpoints(self, watches):
        for w in watches:
            if not self.gdb.set_watchpoint(w.addr, w.length, w.kind):
                raise IOError(f"stub refused a {w.kind} watchpoint at {w.addr:#x}+{w.length}")
            self.watches.append(w)

    def clear_watchpoints(self):
        for w in self.watches:
            self.gdb.remove_watchpoint(w.addr, w.length, w.kind)
        self.watches.clear()

    def _run_stops(self, ms: int, max_hits: int):
        """Yield (stop, regs) for each trap until ``ms`` passes; leaves the game stopped."""
        end, n = time.time() + ms / 1000, 0
        while n < max_hits:
            left = end - time.time()
            if left <= 0:
                self.gdb.interrupt()
                return
            reply = self.gdb.cont(timeout=left)
            if not reply:
                self.gdb.interrupt()
                return
            self.last_stop = reply
            stop = parse_stop(reply)
            if not stop.is_trap:                       # fault or exit: a result, stop here
                return
            n += 1
            yield stop, self.gdb.registers()

    def watch_hits(self, ms, max_hits=500):
        hits = []
        for stop, regs in self._run_stops(ms, max_hits):
            if stop.watch_addr is None:                # a stray breakpoint, not ours to count
                continue
            f, how, stack = self.attribute(regs)
            hits.append(WatchHit(func=f, pc=regs["eip"], kind=stop.watch_kind, addr=stop.watch_addr,
                                 attributed_by=how, stack=stack))
        return hits

    # ------------------------------------------------------------ DOS file calls
    def _decode_file_call(self, via: str, regs: dict[str, int]) -> FileAccess | None:
        if via == "int21":
            r = regs
            fn = (r["eax"] >> 8) & 0xFF
            name_ptr = r.get(DOS_FILE_FNS[fn]) if DOS_FILE_FNS.get(fn) else None
        else:                                          # DPMI 0300h: simulate real-mode int BL
            if regs["eax"] & 0xFFFF != 0x0300 or regs["ebx"] & 0xFF != 0x21:
                return None
            try:
                rm = self.gdb.read_mem(regs["edi"], 0x26)
            except IOError:
                return None
            esi, ebx, edx, _, eax = struct.unpack_from("<I8xIIII", rm, 4)
            ds = struct.unpack_from("<H", rm, 0x24)[0]
            r = {"eax": eax, "ebx": ebx, "ecx": struct.unpack_from("<I", rm, 24)[0]}
            fn = (eax >> 8) & 0xFF
            reg = DOS_FILE_FNS.get(fn)
            name_ptr = ds * 16 + ((edx if reg == "edx" else esi) & 0xFFFF) if reg else None
        if fn not in DOS_FILE_FNS:
            return None
        f, how, stack = self.attribute(regs)
        handle = r["ebx"] & 0xFFFF if fn in DOS_HANDLE_FNS else None
        count = r["ecx"] if fn in (0x3F, 0x40) else None
        return FileAccess(func=f, pc=regs["eip"], via=via, dos_fn=fn,
                          filename=self.gdb.read_cstr(name_ptr) if name_ptr is not None else None,
                          handle=handle, count=count, attributed_by=how, stack=stack)

    def file_accesses(self, ms, sites, max_hits=200):
        addrs = {self.amap.to_runtime(p): via for p, via in sites.items()}
        placed = [a for a in addrs if self.gdb.set_breakpoint(a)]
        out = []
        try:
            for _, regs in self._run_stops(ms, max_hits):
                via = addrs.get(regs["eip"])
                if via is None:
                    continue
                if (fa := self._decode_file_call(via, regs)) is not None:
                    out.append(fa)
                self._step_over_break(regs["eip"])
        finally:
            for a in placed:
                self.gdb.remove_breakpoint(a)
        return out

    # ------------------------------------------------------------ sound-card port writes
    def start_audio_trace(self):
        if self.cfg.kind != "qemu" or not self.cfg.audio_trace or self._trace_ok is False:
            return False
        from .bringup import monitor
        fd, path = tempfile.mkstemp(prefix="ffelab-ports-", suffix=".log", dir=self.cfg.trace_dir or None)
        os.close(fd)
        monitor(f"logfile {path}", self.cfg.monitor_port)
        out = monitor(f"log trace:{TRACE_EVENT}", self.cfg.monitor_port)
        if out.strip():                                # prints its help line: no log trace backend
            self._trace_ok = False
            os.unlink(path)
            return False
        self._trace_ok, self._trace_path = True, path
        return True

    def stop_audio_trace(self):
        if not self._trace_path:
            return None
        from .bringup import monitor
        monitor("log none", self.cfg.monitor_port)    # also closes (flushes) the log file
        path, self._trace_path = self._trace_path, None
        try:
            with open(path, errors="replace") as fh:
                return count_port_writes(fh)
        finally:
            os.unlink(path)


def count_port_writes(lines) -> dict[str, int]:
    """Writes per sound device in a ``memory_region_ops_write`` trace log."""
    counts = {d: 0 for d in AUDIO_DEVICES}
    for ln in lines:
        if TRACE_EVENT in ln and (m := _TRACE_RE.search(ln)) and m.group(2) in counts:
            counts[m.group(2)] += 1
    return counts
