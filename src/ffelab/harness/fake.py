"""A deterministic stand-in for the game, for tests and agent dry runs.

You give it a 'ground truth': which functions fire per scenario (and how
often), and optionally what each function draws. It behaves like an
instrumented game: logging breakpoints count hits, halting breakpoints return
a HaltSnapshot, knocked-out functions stop drawing (or crash the game), and an
optional animated region changes between screenshots to mimic natural motion.

Beyond the screen: ``sound`` gives each function's sound-card writes per run
(knocked-out functions go quiet), ``writers`` which functions write which
addresses (for watchpoints), and ``files`` the DOS file calls each scenario makes.
"""
from __future__ import annotations

from ..halt import HaltSnapshot
from ..models import FileAccess, WatchHit
from .backend import GameBackend

Box = tuple[int, int, int, int]


class FakeBackend(GameBackend):
    def __init__(self, truth: dict[str, dict[int, float]], context: dict[str, list[str]] | None = None,
                 frames_per_run: int = 100, callers: dict[int, list[int]] | None = None,
                 visuals: dict[str, list[tuple[int, Box, tuple[int, int, int]]]] | None = None,
                 crashers: set[int] | None = None, animated: Box | None = None,
                 size: tuple[int, int] = (320, 200),
                 sound: dict[str, dict[int, dict[str, int]]] | None = None,
                 writers: dict[str, dict[int, list[int]]] | None = None,
                 files: dict[str, list[tuple[int, int, str | None]]] | None = None):
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
        self.sound = sound or {}                # scenario -> {func: {device: writes per run}}
        self.writers = writers or {}            # scenario -> {addr: [funcs writing it]}
        self.files = files or {}                # scenario -> [(func, dos_fn, filename)]
        self.watches = []
        self._tracing = False

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

    def set_watchpoints(self, watches):
        self.watches += watches

    def clear_watchpoints(self):
        self.watches = []

    def watch_hits(self, ms, max_hits=500):
        hits = []
        for addr, funcs in sorted(self.writers.get(self.state, {}).items()):
            for w in self.watches:
                if w.addr <= addr < w.addr + w.length and w.kind in ("write", "access"):
                    hits += [WatchHit(func=f, pc=0, kind=w.kind, addr=addr, attributed_by="pc")
                             for f in funcs if f not in self.knocked]
        return hits[:max_hits]

    def file_accesses(self, ms, sites, max_hits=200):
        return [FileAccess(func=f, pc=0, via="int21", dos_fn=fn, filename=name, attributed_by="stack",
                           stack=[f]) for f, fn, name in self.files.get(self.state, [])][:max_hits]

    def start_audio_trace(self):
        self._tracing = bool(self.sound)
        return self._tracing

    def stop_audio_trace(self):
        if not self._tracing:
            return None
        self._tracing = False
        out: dict[str, int] = {}
        for f, devs in self.sound.get(self.state, {}).items():
            for d, n in devs.items():
                out[d] = out.get(d, 0) + (0 if f in self.knocked else n)
        return out

    def health(self):
        return "crashed" if self.knocked & self.crashers else "ok"
