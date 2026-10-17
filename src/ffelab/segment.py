"""Recover original source-file boundaries from the address-ordered function list.

Premise: Braben/Sawyer's assembler output was linked file-by-file, so each
original source file is a contiguous run of functions. For every *cut* (the
gap between two adjacent functions) we compute evidence that a file boundary
sits there, then learn how to weigh that evidence from the names earlier
hackers already assigned.

Features per cut c (between ordered[c-1] and ordered[c]):
  pad         left function ends in nop/int3 filler
  align_shift change in the share of 4-byte-aligned starts (window left vs right)
  frame_shift change in the share of push ebp/mov ebp,esp prologues
  call_clean  calls stay on their own side of the cut (local modularity)
  data_shift  left and right windows touch different DATA labels
  in_anchor   cut would split a module event table's handlers (strong veto)

Learning: we only know module membership at *named* functions. Between two
consecutive named functions with different prefixes there must be at least one
boundary; with the same prefix, none. That is a multiple-instance problem, so
cut probabilities are combined with a noisy-OR and fitted by maximum
likelihood (L2-regularised logistic weights).
"""
from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from .asm import Function, address_ordered
from .tables import Table

FEATURES = ["pad", "align_shift", "frame_shift", "call_clean", "data_shift", "in_anchor"]

# Prefixes that name a subsystem (as opposed to verbs like Get/Find/Add).
MODULE_FAMILIES = {
    "SysMenu": "SysMenu", "CdrFile": "CdrFile", "Console": "Console",
    "String": "String", "StringExpand": "String", "StringConvert": "String",
    "StringDraw": "String", "FF30": "String", "FF80": "String",
    "Int64": "Int64", "Vec64": "Vec64", "Mat": "Mat", "DPal": "DPal",
    "Keyb": "Keyb", "Gui": "Gui", "Timer": "Timer", "Mouse": "Mouse",
    "ScrnShot": "ScrnShot", "Scrnshot": "ScrnShot", "Sound": "Sound",
    "Video": "Video", "Draw": "Draw", "Blit": "Blit", "Tex": "Tex", "Text": "Text",
    "Spawn": "Spawn",
}


def family(f: Function) -> str | None:
    p = f.prefix()
    return MODULE_FAMILIES.get(p) if p else None


@dataclass
class Segmentation:
    ordered: list[Function]
    X: np.ndarray                     # (n_cuts, n_features); row c-1 describes cut c
    weights: np.ndarray
    bias: float
    cut_prob: np.ndarray              # probability per cut
    boundaries: list[int]             # chosen cut indices c
    anchors: list[tuple[int, int, Table]]
    report: dict = field(default_factory=dict)


# ---------------------------------------------------------------- features
def _window_mean(vals: np.ndarray, lo: int, hi: int) -> float:
    lo, hi = max(lo, 0), min(hi, len(vals))
    return float(vals[lo:hi].mean()) if hi > lo else 0.0


def compute_features(ordered: list[Function], anchors: list[tuple[int, int, Table]],
                     window: int = 8) -> np.ndarray:
    n = len(ordered)
    ord_of = {f.num: i for i, f in enumerate(ordered)}
    aligned = np.array([1.0 if f.aligned4 else 0.0 for f in ordered])
    frame = np.array([1.0 if f.has_frame else 0.0 for f in ordered])
    edges = set()
    for f in ordered:
        a = ord_of[f.num]
        for c in f.calls + f.tail_jumps:
            b = ord_of.get(c)
            if b is not None and b != a:
                edges.add((min(a, b), max(a, b)))
    edges_arr = np.array(sorted(edges)) if edges else np.zeros((0, 2), int)
    data = [set(f.data_refs) for f in ordered]

    X = np.zeros((n - 1, len(FEATURES)))
    for c in range(1, n):
        lo, hi = c - window, c + window
        X[c - 1, 0] = 1.0 if ordered[c - 1].filler_tail > 0 else 0.0
        X[c - 1, 1] = abs(_window_mean(aligned, lo, c) - _window_mean(aligned, c, hi))
        X[c - 1, 2] = abs(_window_mean(frame, lo, c) - _window_mean(frame, c, hi))
        if len(edges_arr):
            a, b = edges_arr[:, 0], edges_arr[:, 1]
            near = (a >= lo) & (b < hi)
            crossing = int(np.sum(near & (a < c) & (b >= c)))
            internal = int(np.sum(near)) - crossing
            X[c - 1, 3] = (internal - crossing) / (internal + crossing + 1)
        left = set().union(*data[max(lo, 0):c])
        right = set().union(*data[c:min(hi, n)])
        union = left | right
        X[c - 1, 4] = 1.0 - (len(left & right) / len(union)) if union else 0.0
        X[c - 1, 5] = 1.0 if any(s < c <= e for s, e, _ in anchors) else 0.0
    return X


def anchor_ranges(ordered: list[Function], tables: list[Table]) -> list[tuple[int, int, Table]]:
    ord_of = {f.num: i for i, f in enumerate(ordered)}
    out = []
    for t in tables:
        if t.kind != "event_table":
            continue
        ords = [ord_of[n] for n in t.functions if n in ord_of]
        if ords:
            out.append((min(ords), max(ords), t))
    return sorted(out, key=lambda r: r[0])


# ---------------------------------------------------------------- labels
def labelled_pairs(ordered: list[Function]) -> list[tuple[int, int, int]]:
    """(cut_lo, cut_hi, label): cuts lo..hi inclusive lie between two consecutive
    family-named functions; label 1 if their families differ."""
    named = [(i, family(f)) for i, f in enumerate(ordered) if family(f)]
    pairs = []
    for (i, fa), (j, fb) in zip(named, named[1:]):
        pairs.append((i + 1, j, int(fa != fb)))
    return pairs


# ---------------------------------------------------------------- model
def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-z))


def _pair_probs(Xs: np.ndarray, w: np.ndarray, b: float, pairs) -> np.ndarray:
    p = _sigmoid(Xs @ w + b)
    out = np.empty(len(pairs))
    for k, (lo, hi, _) in enumerate(pairs):
        seg = p[lo - 1:hi]                 # cut c is row c-1
        out[k] = 1.0 - np.prod(1.0 - seg)
    return np.clip(out, 1e-9, 1 - 1e-9)


def fit(X: np.ndarray, pairs, l2: float = 1.0, iters: int = 400, lr: float = 0.3,
        mu: np.ndarray | None = None, sd: np.ndarray | None = None):
    """Noisy-OR multiple-instance logistic regression by gradient descent."""
    mu = X.mean(0) if mu is None else mu
    sd = (X.std(0) + 1e-9) if sd is None else sd
    Xs = (X - mu) / sd
    w = np.zeros(X.shape[1]); b = -3.0
    y = np.array([lab for _, _, lab in pairs], float)
    for _ in range(iters):
        p = _sigmoid(Xs @ w + b)
        gw = np.zeros_like(w); gb = 0.0
        for k, (lo, hi, _) in enumerate(pairs):
            seg = slice(lo - 1, hi)
            pc = p[seg]
            P = np.clip(1.0 - np.prod(1.0 - pc), 1e-9, 1 - 1e-9)
            # d logL / d z_c for noisy-OR
            if y[k]:
                coef = ((1.0 - P) / P) * pc        # = (1-P)/P * p_c ... derivative of log P
            else:
                coef = -pc                          # derivative of log(1-P) = -sum p_c
            gw += Xs[seg].T @ coef
            gb += coef.sum()
        gw -= l2 * w
        w += lr * gw / len(pairs); b += lr * gb / len(pairs)
    return w, b, mu, sd


def auc(scores: np.ndarray, labels: np.ndarray) -> float:
    pos, neg = scores[labels == 1], scores[labels == 0]
    if not len(pos) or not len(neg):
        return float("nan")
    gt = (pos[:, None] > neg[None, :]).sum() + 0.5 * (pos[:, None] == neg[None, :]).sum()
    return float(gt / (len(pos) * len(neg)))


def cross_validate(X, pairs, folds: int = 5, seed: int = 0) -> float:
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(pairs))
    scores = np.zeros(len(pairs)); labels = np.array([p[2] for p in pairs])
    for k in range(folds):
        test = idx[k::folds]
        train = np.setdiff1d(idx, test)
        w, b, mu, sd = fit(X, [pairs[i] for i in train])
        Xs = (X - mu) / sd
        scores[test] = _pair_probs(Xs, w, b, [pairs[i] for i in test])
    return auc(scores, labels)


def feature_aucs(X, pairs) -> dict:
    labels = np.array([p[2] for p in pairs])
    out = {}
    for j, name in enumerate(FEATURES):
        s = np.array([X[lo - 1:hi, j].max() for lo, hi, _ in pairs])
        out[name] = round(auc(s, labels), 3)
    return out


# ---------------------------------------------------------------- decode
def decode(cut_prob: np.ndarray, anchors, threshold: float = 0.5, min_gap: int = 2) -> list[int]:
    n_cuts = len(cut_prob)
    inside = np.zeros(n_cuts + 1, bool)
    for s, e, _ in anchors:
        inside[s + 1:e + 1] = True
    cand = [c for c in range(1, n_cuts + 1) if cut_prob[c - 1] >= threshold and not inside[c]]
    # every gap between two consecutive module tables must hold >= 1 boundary
    for (s1, e1, _), (s2, e2, _) in zip(anchors, anchors[1:]):
        gap = [c for c in range(e1 + 1, s2 + 1) if not inside[c]]
        if gap and not any(c in gap for c in cand):
            cand.append(max(gap, key=lambda c: cut_prob[c - 1]))
    cand.sort(key=lambda c: -cut_prob[c - 1])
    chosen: list[int] = []
    for c in cand:
        if all(abs(c - d) >= min_gap for d in chosen):
            chosen.append(c)
    return sorted(chosen)


def pair_scores(bounds: list[int], pairs) -> dict:
    """Precision/recall/F1 of chosen boundaries against the labelled name pairs."""
    bset = set(bounds)
    tp = fp = fn = 0
    for lo, hi, lab in pairs:
        hit = any(c in bset for c in range(lo, hi + 1))
        tp += hit and lab; fp += hit and not lab; fn += (not hit) and lab
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"precision": round(prec, 3), "recall": round(rec, 3), "f1": round(f1, 3)}


def tune_threshold(cut_prob, anchors, pairs) -> tuple[float, dict]:
    best = (0.5, {"f1": -1})
    for q in np.linspace(80, 99.8, 100):
        t = float(np.percentile(cut_prob, q))
        sc = pair_scores(decode(cut_prob, anchors, t), pairs)
        if sc["f1"] > best[1]["f1"]:
            best = (t, sc)
    return best


def segment(funcs: list[Function], tables: list[Table], threshold: float | None = None) -> Segmentation:
    ordered = address_ordered(funcs)
    anchors = anchor_ranges(ordered, tables)
    X = compute_features(ordered, anchors)
    pairs = labelled_pairs(ordered)
    w, b, mu, sd = fit(X, pairs)
    cut_prob = _sigmoid(((X - mu) / sd) @ w + b)
    if threshold is None:
        threshold, _ = tune_threshold(cut_prob, anchors, pairs)
    bounds = decode(cut_prob, anchors, threshold)
    report = {
        "functions": len(ordered),
        "labelled_pairs": len(pairs),
        "positive_pairs": int(sum(p[2] for p in pairs)),
        "feature_auc": feature_aucs(X, pairs),
        "model_cv_auc": round(cross_validate(X, pairs), 3),
        "weights": {n: round(float(v), 3) for n, v in zip(FEATURES, w)},
        "threshold": round(float(threshold), 4),
        "boundaries": len(bounds),
        "pair_fit_in_sample": pair_scores(bounds, pairs),
        "event_tables": len(anchors),
    }
    return Segmentation(ordered, X, w, b, cut_prob, bounds, anchors, report)


def segments_table(seg: Segmentation) -> list[dict]:
    cuts = [0] + seg.boundaries + [len(seg.ordered)]
    out = []
    for k, (a, z) in enumerate(zip(cuts, cuts[1:])):
        members = seg.ordered[a:z]
        fams = Counter(family(f) for f in members if family(f))
        named = [f.name for f in members if f.name]
        anchors = [t.comment or t.label for s, e, t in seg.anchors if a <= s and e < z]
        label = (anchors[0] if anchors else
                 (fams.most_common(1)[0][0] if fams else None))
        out.append({
            "seg": k,
            "first": members[0].num, "last": members[-1].num,
            "pos_first": hex(members[0].pos), "pos_last": hex(members[-1].pos),
            "size": len(members),
            "boundary_conf": None if a == 0 else round(float(seg.cut_prob[a - 1]), 3),
            "label": label,
            "anchors": anchors,
            "families": dict(fams),
            "named_examples": named[:6],
        })
    return out


def family_integrity(seg: Segmentation) -> list[dict]:
    """For each contiguous family block, how many chosen boundaries fall inside it."""
    blocks = []
    cur = None
    for i, f in enumerate(seg.ordered):
        fam = family(f)
        if fam is None:
            continue
        if cur and cur["family"] == fam:
            cur["last"] = i; cur["n"] += 1
        else:
            cur = {"family": fam, "first": i, "last": i, "n": 1}
            blocks.append(cur)
    out = []
    bset = set(seg.boundaries)
    for b in blocks:
        if b["n"] < 3:
            continue
        inside = sum(1 for c in range(b["first"] + 1, b["last"] + 1) if c in bset)
        out.append({"family": b["family"], "named": b["n"],
                    "span": b["last"] - b["first"] + 1, "boundaries_inside": inside})
    return out


def to_json(seg: Segmentation) -> str:
    return json.dumps({"report": seg.report, "segments": segments_table(seg),
                       "family_integrity": family_integrity(seg)}, indent=2)
