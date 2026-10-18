"""Breakpoint halts as evidence.

A halt freezes the game at one function. ``HaltSnapshot`` is the standard
record of everything captured there; ``score_halt`` turns it into a
likelihood over subsystem labels for the ledger. (The model itself
lives in ``models.py`` with the other records.)

Each rule multiplies in a modest factor (1.3x-3x) so no single halt can
dominate a belief: many consistent halts move it, one weird halt doesn't.
The factors are deliberately simple and live in ``RULES`` so you can tune
them as your calibration data comes in.
"""
from __future__ import annotations

from .ledger import DEFAULT_LABELS
from .models import HaltSnapshot  # noqa: F401  (re-exported; defined with the other records)


RULES = {
    "frequency": {
        "per_frame": {"render": 2.5, "physics": 2.5, "ai": 1.8, "platform": 1.8, "math": 1.5, "audio": 1.3},
        "rare": {"ui": 1.8, "mission": 1.8, "sysgen": 1.8, "io": 1.8, "economy": 1.5, "text": 1.3},
    },
    "context": {
        "docked": {"economy": 2.0, "ui": 1.7, "mission": 1.5, "text": 1.3},
        "menu": {"ui": 2.5, "io": 1.5, "text": 1.5},
        "combat": {"ai": 2.0, "physics": 1.7, "render": 1.3, "audio": 1.3},
        "hyperspace": {"sysgen": 2.5, "physics": 1.3},
        "planet_surface": {"render": 2.0, "physics": 1.5},
        "loading": {"io": 2.5, "platform": 1.5},
    },
    "trigger": {                                # FFE's F-key groups (adjust from your knowledge)
        "F1": {"ui": 1.5, "physics": 1.3}, "F2": {"ui": 2.0}, "F3": {"ui": 1.5, "economy": 1.5},
        "F4": {"ui": 1.5, "text": 1.5}, "F5": {"ui": 1.5, "mission": 1.3},
        "Esc": {"ui": 2.0, "io": 1.3},
    },
    "region": {
        "object_array": {"ai": 1.8, "physics": 1.8, "render": 1.3},
        "stock_data": {"economy": 2.5},
        "star_data": {"sysgen": 2.5},
        "mission_data": {"mission": 2.5},
        "screen_buffer": {"render": 2.5, "platform": 1.5},
        "string_table": {"text": 2.0, "ui": 1.5},
        "sample_data": {"audio": 3.0},
    },
    "value": {
        "ascii": {"text": 2.0, "ui": 1.5},
        "int64": {"physics": 1.8, "math": 1.8},
        "fixed16": {"physics": 1.5, "math": 1.5, "render": 1.3},
        "rgb": {"render": 2.0},
    },
}


def score_halt(h: HaltSnapshot, module_label: dict[int, str] | None = None,
               labels: list[str] | None = None) -> dict[str, float]:
    """Likelihood over labels for one halt. 1.0 means 'no information'."""
    labels = labels or DEFAULT_LABELS
    like = {lab: 1.0 for lab in labels}

    def apply(factors: dict[str, float]):
        for lab, f in factors.items():
            if lab in like:
                like[lab] *= f

    hpf = h.hits_per_frame
    if hpf >= 0.9:
        apply(RULES["frequency"]["per_frame"])
    elif h.hits <= 3:
        apply(RULES["frequency"]["rare"])
    for tag in h.context:
        apply(RULES["context"].get(tag, {}))
    if h.trigger:
        apply(RULES["trigger"].get(h.trigger, {}))
    for region in h.pointer_regions:
        apply(RULES["region"].get(region, {}))
    for vk in h.value_kinds:
        apply(RULES["value"].get(vk, {}))
    # call stack: the nearest caller with a known module label is strong evidence
    if module_label:
        for depth, caller in enumerate(h.stack[:6]):
            lab = module_label.get(caller)
            if lab in like:
                like[lab] *= 3.0 / (1 + 0.5 * depth)
                break
    return like


def score_never_hit(context: list[str], labels: list[str] | None = None) -> dict[str, float]:
    """A function that never fired in a scenario is evidence *against* labels
    that scenario exercises (inverse of the context factors, softened)."""
    labels = labels or DEFAULT_LABELS
    like = {lab: 1.0 for lab in labels}
    for tag in context:
        for lab, f in RULES["context"].get(tag, {}).items():
            if lab in like:
                like[lab] /= f ** 0.5
    return like
