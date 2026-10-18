"""Drive a real emulator: window input/screenshots on macOS + GDB-protocol debugging.

Two supported setups:

* **QEMU + FreeDOS** (most scriptable): ``qemu-system-i386 ... -gdb tcp::1234
  -monitor tcp:127.0.0.1:4444,server,nowait``. Breakpoints over GDB, save
  states via the monitor (``savevm`` / ``loadvm``), input via the monitor's
  ``sendkey``. No window automation needed.
* **DOSBox-X** (best compatibility): input via macOS System Events, screenshots
  via ``screencapture``, save states via DOSBox-X's own hotkeys (set in
  config), debugging via a GDB stub if your build has one.

UNTESTED against a live emulator so far: the protocol pieces are unit-tested
against a fake stub; expect to adjust key maps and timings once.
"""
from __future__ import annotations

import bisect
import socket
import struct
import subprocess
import time
from dataclasses import dataclass, field

from ..halt import HaltSnapshot
from .backend import AddressMap, GameBackend
from .gdb import GdbRemote

# macOS virtual key codes for keys FFE uses
MAC_KEYCODES = {
    "F1": 122, "F2": 120, "F3": 99, "F4": 118, "F5": 96, "F6": 97, "F7": 98, "F8": 100,
    "F9": 101, "F10": 109, "F11": 103, "F12": 111, "Esc": 53, "Enter": 36, "Space": 49,
    "Tab": 48, "Left": 123, "Right": 124, "Down": 125, "Up": 126,
}
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


class EmulatorBackend(GameBackend):
    def __init__(self, cfg: EmulatorConfig, func_positions: dict[int, int]):
        self.cfg = cfg
        self.amap = AddressMap(cfg.code_base)
        self.gdb = GdbRemote(cfg.gdb_host, cfg.gdb_port)
        self.func_pos = func_positions                       # func num -> Pos
        self._sorted = sorted((p, f) for f, p in func_positions.items())
        self._starts = [p for p, _ in self._sorted]
        self.breaks: dict[int, int] = {}                     # runtime addr -> func
        self.counts: dict[int, int] = {}
        self.log_only = False
        self.originals: dict[int, bytes] = {}             # func -> bytes replaced by knock_out
        self.last_stop = ""

    # ------------------------------------------------------------ helpers
    def func_at(self, addr: int) -> int | None:
        """Function containing runtime address ``addr`` (nearest start below)."""
        pos = self.amap.to_pos(addr)
        i = bisect.bisect_right(self._starts, pos) - 1
        return self._sorted[i][1] if i >= 0 else None

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

    def run_until_halt(self, timeout_ms, scenario=""):
        reply = self.gdb.cont(timeout=timeout_ms / 1000)
        if not reply:
            self.gdb.interrupt()
            return None
        regs = self.gdb.registers()
        f = self.breaks.get(regs["eip"]) or self.func_at(regs["eip"])
        esp = regs["esp"]
        try:
            args = list(struct.unpack("<4I", self.gdb.read_mem(esp + 4, 16)))
        except IOError:
            args = []
        return HaltSnapshot(func=f, scenario=scenario, hits=self.counts.get(f, 1),
                            stack=self.stack(regs), regs=regs, args=args)

    def resume(self):
        self.gdb.cont(timeout=0.01)

    def read_mem(self, addr, length):
        return self.gdb.read_mem(addr, length)
