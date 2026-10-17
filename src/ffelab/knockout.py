"""Knockout experiments: disable a function and see what disappears.

When observation can't tell you what a function is (no names, no hints), turn
it off. The procedure, from the same save state each time:

  1. baseline A: load state, play the keys, wait, screenshot
  2. baseline B: same again (natural motion between A and B = noise mask)
  3. knockout:  load state, patch the function's entry to RET, same keys,
                wait, check health, screenshot, restore
  4. diff knockout vs A, ignoring pixels that already differed between A and B

Results:
  * crashed / frozen   -> something depends on its output (weak evidence toward
                          core systems); a fact worth knowing either way
  * no visible change  -> evidence against render/ui/text in this scenario
  * visible change     -> a diff image to look at

The first two are scored automatically. The third needs eyes: the agent (or
you) looks at the diff image and records an *observation* with a label path
such as ``render/planet/atmosphere/clouds``. Observations are tied to the
experiment and its images, so anyone can audit them later.
"""
from __future__ import annotations

import json
import os

from . import labels as lbl
from .harness.backend import GameBackend
from .ledger import Ledger

VISIBLE_THRESHOLD = 0.0005          # fraction of changed pixels counted as a visible effect
PIXEL_DELTA = 24                    # per-pixel intensity change counted as 'changed'

LIKE_CRASH = {"platform": 1.5, "math": 1.4, "physics": 1.3, "io": 1.2, "ai": 1.1,
              "render": 0.9, "ui": 0.9, "text": 0.9, "audio": 0.8}
LIKE_INVISIBLE = {"render": 0.4, "ui": 0.5, "text": 0.6}
LIKE_VISIBLE = {"render": 2.0, "ui": 1.5, "text": 1.3}


def image_diff(a_path: str, b_path: str, out_path: str | None = None,
               noise_path: str | None = None) -> dict:
    """Changed-pixel fraction and bounding box between two screenshots.

    ``noise_path``: a second baseline; pixels that differ between it and ``a``
    are natural motion and ignored. Writes ``out_path`` (b with changed pixels in red)."""
    from PIL import Image, ImageChops

    a = Image.open(a_path).convert("RGB")
    b = Image.open(b_path).convert("RGB")
    if a.size != b.size:
        b = b.resize(a.size)
    changed = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > PIXEL_DELTA else 0)
    if noise_path:
        n = Image.open(noise_path).convert("RGB").resize(a.size)
        noise = ImageChops.difference(a, n).convert("L").point(lambda v: 255 if v > PIXEL_DELTA else 0)
        changed = ImageChops.subtract(changed, noise)
    hist = changed.histogram()
    n_changed = hist[255]
    total = a.size[0] * a.size[1]
    bbox = changed.getbbox()
    if out_path:
        overlay = b.copy()
        red = Image.new("RGB", a.size, (255, 0, 0))
        overlay.paste(red, mask=changed)
        overlay.save(out_path)
    return {"changed_frac": n_changed / total, "changed_pixels": n_changed,
            "bbox": list(bbox) if bbox else None, "diff_image": out_path}


def knockout(backend: GameBackend, ledger: Ledger, scenario: str, func: int,
             keys: list[str] | None = None, settle_ms: int = 1500, outdir: str = "knockouts",
             patch: bytes = b"\xc3") -> dict:
    os.makedirs(outdir, exist_ok=True)
    stem = os.path.join(outdir, f"{scenario}_F{func}")
    paths = {k: f"{stem}_{k}.png" for k in ("baseA", "baseB", "knocked", "diff")}

    def play():
        backend.load_state(scenario)
        if keys:
            backend.press_keys(keys)
        backend.wait(settle_ms)

    backend.clear_breakpoints()
    play(); backend.screenshot(paths["baseA"])
    play(); backend.screenshot(paths["baseB"])
    backend.load_state(scenario)
    backend.knock_out(func, patch)
    try:
        if keys:
            backend.press_keys(keys)
        backend.wait(settle_ms)
        health = backend.health()
        backend.screenshot(paths["knocked"])
    finally:
        backend.restore(func)
        backend.load_state(scenario)             # leave the game in a clean state

    result = {"func": func, "scenario": scenario, "health": health, "images": paths}
    if health == "ok":
        d = image_diff(paths["baseA"], paths["knocked"], paths["diff"], noise_path=paths["baseB"])
        result.update(d)
        outcome = "visible" if d["changed_frac"] >= VISIBLE_THRESHOLD else "invisible"
    else:
        result.update({"changed_frac": None, "bbox": None, "diff_image": None})
        outcome = "crash"
    result["outcome"] = outcome

    exp = ledger.record_experiment("knockout", scenario, {
        "func": func, "keys": keys, "patch": patch.hex(), "health": health, "outcome": outcome,
        "changed_frac": result.get("changed_frac"), "bbox": result.get("bbox"), "images": paths})
    ledger.con.execute(
        "INSERT INTO knockouts (experiment_id, func, scenario, health, changed_frac, bbox, baseline, knocked, diff_image)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (exp, func, scenario, health, result.get("changed_frac"), json.dumps(result.get("bbox")),
         paths["baseA"], paths["knocked"], result.get("diff_image")))
    ledger.con.commit()

    like = {"crash": LIKE_CRASH, "invisible": LIKE_INVISIBLE, "visible": LIKE_VISIBLE}[outcome]
    ledger.update(func, like, "knockout", "knockout",
                  f"{scenario}: {outcome} (changed {result.get('changed_frac')})", strength=0.8,
                  experiment_id=exp)
    result["experiment"] = exp
    result["resolved"] = _resolve_knockout_bets(ledger, exp, scenario, func, outcome)
    result["next"] = ("look at the diff image and call record_knockout_observation"
                      if outcome == "visible" else None)
    return result


def _resolve_knockout_bets(ledger: Ledger, exp: int, scenario: str, func: int, outcome: str) -> list[dict]:
    done = []
    for bet in ledger.con.execute("SELECT * FROM bets WHERE outcome IS NULL AND spec IS NOT NULL").fetchall():
        spec = json.loads(bet["spec"])
        if not spec.get("knockout") or spec.get("func") != func or spec.get("scenario") != scenario:
            continue
        hit = spec.get("expect") == outcome
        done.append({"bet": bet["id"], "outcome": hit, "brier": ledger.resolve_bet(bet["id"], hit, exp)})
    return done


def record_observation(ledger: Ledger, experiment_id: int, func: int, path: str, confidence: float,
                       description: str, source: str) -> dict:
    """What disappeared or changed in a knockout, as a label path, tied to the experiment."""
    exp = ledger.con.execute("SELECT * FROM experiments WHERE id=?", (experiment_id,)).fetchone()
    if exp is None or exp["kind"] != "knockout":
        raise ValueError("observations must cite a knockout experiment")
    detail = json.loads(exp["detail"])
    if detail.get("func") != func:
        raise ValueError(f"knockout {experiment_id} disabled FUNC {detail.get('func')}, not {func}")
    if detail.get("outcome") != "visible":
        raise ValueError("only knockouts with a visible change can carry a visual observation")
    path = lbl.validate(path, ledger.labels)
    lbl.register(ledger.con, path, source, description)
    ledger.con.execute(
        "INSERT INTO func_labels (func, path, source, confidence, experiment_id, note) VALUES (?,?,?,?,?,?)",
        (func, path, source, confidence, experiment_id, description))
    r = ledger.reliability(source)
    s = max(0.0, min(1.0, confidence * r))
    k = len(ledger.labels)
    top = lbl.top(path)
    like = {lab: (s + (1 - s) / k) * k if lab == top else (1 - s) / k * k for lab in ledger.labels}
    post = ledger.update(func, like, source, "knockout_obs", f"{path}: {description}",
                         experiment_id=experiment_id)
    return {"func": func, "path": path, "belief_top": max(post, key=post.get),
            "p": round(max(post.values()), 3)}
