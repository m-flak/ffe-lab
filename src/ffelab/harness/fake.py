"""A deterministic stand-in for the game, for tests and agent dry runs.

You give it a 'ground truth': which functions fire per scenario (and how
often), and optionally what each function draws. It behaves like an
instrumented game: logging breakpoints count hits, halting breakpoints return
a HaltSnapshot, knocked-out functions stop drawing (or crash the game), and an
optional animated region changes between screenshots to mimic natural motion.
"""
from __future__ import annotations

from ..halt import HaltSnapshot
from .backend import GameBackend

Box = tuple[int, int, int, int]


class FakeBackend(GameBackend):
    def __init__(self, truth: dict[str, dict[int, float]], context: dict[str, list[str]] | None = None,
                 frames_per_run: int = 100, callers: dict[int, list[int]] | None = None,
                 visuals: dict[str, list[tuple[int, Box, tuple[int, int, int]]]] | None = None,
                 crashers: set[int] | None = None, animated: Box | None = None,
                 size: tuple[int, int] = (320, 200)):
        self.truth = truth                      # scenario -> {func: hits per frame}
        self.context = context or {}
        self.frames = frames_per_run
        self.callers = callers or {}
        self.visuals = visuals or {}            # scenario -> [(func, box, rgb)]
        self.crashers = crashers or set()
        self.animated = animated                # region that flickers between shots
        self.size = size
        self.state = next(iter(truth))
        self.breaks: set[int] = set()
        self.log_only = False
        self.keys: list[str] = []
        self.saved: dict[str, str] = {}
        self.knocked: set[int] = set()
        self.shots = 0

    def load_state(self, name):
        if name not in self.truth and name not in self.saved:
            raise KeyError(f"no save state {name!r}")
        self.state = self.saved.get(name, name)

    def save_state(self, name):
        self.saved[name] = self.state

    def press_keys(self, keys, hold_ms=80, gap_ms=120):
        self.keys += keys

    def wait(self, ms):
        pass

    def screenshot(self, path):
        self.shots += 1
        if not self.visuals:
            with open(path, "w") as fh:
                fh.write(f"fake screenshot of {self.state}\n")
            return path
        from PIL import Image, ImageDraw
        img = Image.new("RGB", self.size, (0, 0, 16))
        d = ImageDraw.Draw(img)
        if self.health() == "ok":
            for func, box, rgb in self.visuals.get(self.state, []):
                if func not in self.knocked:
                    d.rectangle(box, fill=rgb)
            if self.animated:
                shade = 60 + 40 * (self.shots % 3)
                d.rectangle(self.animated, fill=(shade, shade, shade))
        img.save(path)
        return path

    def set_breakpoints(self, funcs, log_only=False):
        self.breaks |= set(funcs); self.log_only = log_only

    def clear_breakpoints(self):
        self.breaks.clear()

    def hit_counts(self):
        rates = self.truth[self.state]
        return {f: int(round(rates.get(f, 0.0) * self.frames)) for f in self.breaks}

    def run_until_halt(self, timeout_ms, scenario=""):
        rates = self.truth[self.state]
        live = [f for f in sorted(self.breaks) if rates.get(f, 0) > 0]
        if self.log_only or not live:
            return None
        f = max(live, key=lambda x: rates[x])
        return HaltSnapshot(func=f, scenario=scenario or self.state,
                            hits=int(round(rates[f] * self.frames)), frames=self.frames,
                            stack=self.callers.get(f, []), context=self.context.get(self.state, []),
                            trigger=self.keys[-1] if self.keys else None)

    def read_mem(self, addr, length):
        return bytes(length)

    def knock_out(self, func, patch=b"\xc3"):
        self.knocked.add(func)

    def restore(self, func):
        self.knocked.discard(func)

    def health(self):
        return "crashed" if self.knocked & self.crashers else "ok"
