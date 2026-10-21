"""What the agent can do to the running game, independent of emulator.

The agent plays through these calls: load a save state, press keys, look at
the screen, set breakpoints on FFE functions, run until a halt, read memory.
Backends: ``FakeBackend`` (tests, dry runs) and ``EmulatorBackend``
(DOSBox-X/QEMU window control + a GDB-protocol debugger).

Beyond the screen (optional; a backend that can't do one raises
NotImplementedError or, for the audio trace, returns False/None): memory
watchpoints, DOS file calls caught at the game's ``int 21h``/``int 31h`` sites,
and counts of sound-card port writes while the game runs.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..halt import HaltSnapshot
from ..models import FileAccess, Watch, WatchHit

# Longest possible x86 instruction. A function can't extend further past its
# start than this times its instruction count, so addresses beyond that are
# unmapped code (the C runtime, HMI sound code, DOS/4GW), not that function.
MAX_INSN_BYTES = 15


@dataclass
class AddressMap:
    """Maps ffe.asm ``Pos`` offsets to runtime linear addresses.

    ``base`` is where the code image sits in emulated memory. Calibrate it
    once: halt anywhere, read the bytes at a known function's runtime address
    and confirm they match that function's prologue.
    """
    base: int = 0

    def to_runtime(self, pos: int) -> int:
        return self.base + pos

    def to_pos(self, addr: int) -> int:
        return addr - self.base


class GameBackend(ABC):
    @abstractmethod
    def load_state(self, name: str) -> None: ...

    @abstractmethod
    def save_state(self, name: str) -> None: ...

    @abstractmethod
    def press_keys(self, keys: list[str], hold_ms: int = 80, gap_ms: int = 120) -> None: ...

    @abstractmethod
    def wait(self, ms: int) -> None: ...

    @abstractmethod
    def screenshot(self, path: str) -> str: ...

    @abstractmethod
    def set_breakpoints(self, funcs: list[int], log_only: bool = False) -> None: ...

    @abstractmethod
    def clear_breakpoints(self) -> None: ...

    @abstractmethod
    def run_until_halt(self, timeout_ms: int, scenario: str = "") -> HaltSnapshot | None: ...

    @abstractmethod
    def hit_counts(self) -> dict[int, int]:
        """Hits per function since the last clear, for logging breakpoints."""

    @abstractmethod
    def read_mem(self, addr: int, length: int) -> bytes: ...

    @abstractmethod
    def knock_out(self, func: int, patch: bytes = b"\xc3") -> None:
        """Patch ``func``'s entry so it returns immediately (default: RET)."""

    @abstractmethod
    def restore(self, func: int) -> None:
        """Undo a knock_out."""

    # ------------------------------------------------------------ beyond the screen
    def set_watchpoints(self, watches: list[Watch]) -> None:
        raise NotImplementedError(f"{type(self).__name__} has no watchpoints")

    def clear_watchpoints(self) -> None:
        pass

    def watch_hits(self, ms: int, max_hits: int = 500) -> list[WatchHit]:
        """Run for ``ms`` (or until ``max_hits``) and record every watchpoint stop."""
        raise NotImplementedError(f"{type(self).__name__} has no watchpoints")

    def file_accesses(self, ms: int, sites: dict[int, str], max_hits: int = 200) -> list[FileAccess]:
        """Run for ``ms`` with breakpoints on ``sites`` (Pos -> "int21" | "int31") and decode DOS file calls."""
        raise NotImplementedError(f"{type(self).__name__} can't watch file access")

    def start_audio_trace(self) -> bool:
        """Start counting sound-card port writes; False if this backend can't."""
        return False

    def stop_audio_trace(self) -> dict[str, int] | None:
        """Stop counting; writes per sound device (e.g. {"sb16": 812, "adlib": 3040})."""
        return None

    def health(self) -> str:
        """'ok', 'crashed' (fault/exception) or 'frozen' (no progress)."""
        return "ok"

    def resume(self) -> None:  # optional
        pass
