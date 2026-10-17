"""What the agent can do to the running game, independent of emulator.

The agent plays through these calls: load a save state, press keys, look at
the screen, set breakpoints on FFE functions, run until a halt, read memory.
Backends: ``FakeBackend`` (tests, dry runs) and ``EmulatorBackend``
(DOSBox-X/QEMU window control + a GDB-protocol debugger).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..halt import HaltSnapshot


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

    def health(self) -> str:
        """'ok', 'crashed' (fault/exception) or 'frozen' (no progress)."""
        return "ok"

    def resume(self) -> None:  # optional
        pass
