"""Small, dependency-free classification metrics (exact, deterministic)."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from typing import Any

NONE = "∅"  # unresolved prediction


def per_class(y_true: Sequence[str], y_pred: Sequence[str | None]) -> dict[str, dict[str, float]]:
    tp: Counter[str] = Counter()
    fp: Counter[str] = Counter()
    fn: Counter[str] = Counter()
    for t, p in zip(y_true, y_pred, strict=True):
        if p == t:
            tp[t] += 1
        else:
            fn[t] += 1
            if p is not None:
                fp[p] += 1
    out = {}
    for c in sorted(set(y_true)):
        prec = tp[c] / (tp[c] + fp[c]) if tp[c] + fp[c] else 0.0
        rec = tp[c] / (tp[c] + fn[c]) if tp[c] + fn[c] else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        out[c] = {"precision": prec, "recall": rec, "f1": f1, "support": tp[c] + fn[c]}
    return out


def summary(y_true: Sequence[str], y_pred: Sequence[str | None]) -> dict[str, float]:
    n = len(y_true)
    if n == 0:
        return {"n": 0}
    resolved = [(t, p) for t, p in zip(y_true, y_pred, strict=True) if p is not None]
    correct = sum(t == p for t, p in zip(y_true, y_pred, strict=True))
    pc = per_class(y_true, y_pred)
    return {
        "n": n,
        "accuracy": correct / n,
        "coverage": len(resolved) / n,
        "precision_resolved": (sum(t == p for t, p in resolved) / len(resolved))
        if resolved
        else 0.0,
        "macro_f1": sum(v["f1"] for v in pc.values()) / len(pc) if pc else 0.0,
    }


def confusions(
    y_true: Sequence[str], y_pred: Sequence[str | None], top: int = 10
) -> list[dict[str, Any]]:
    c: Counter[tuple[str, str]] = Counter(
        (t, p if p is not None else NONE) for t, p in zip(y_true, y_pred, strict=True) if p != t
    )
    return [{"true": t, "pred": p, "count": n} for (t, p), n in c.most_common(top)]


def calibration(
    conf: Sequence[float | None],
    ok: Sequence[bool],
    edges: Sequence[float] = (0.0, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0001),
) -> list[dict[str, Any]]:
    buckets: dict[int, list[tuple[float, bool]]] = defaultdict(list)
    for c, o in zip(conf, ok, strict=True):
        if c is None:
            continue
        for i in range(len(edges) - 1):
            if edges[i] <= c < edges[i + 1]:
                buckets[i].append((c, o))
                break
    out = []
    for i in range(len(edges) - 1):
        items = buckets.get(i, [])
        if items:
            out.append(
                {
                    "lo": edges[i],
                    "hi": min(edges[i + 1], 1.0),
                    "n": len(items),
                    "mean_confidence": sum(c for c, _ in items) / len(items),
                    "accuracy": sum(o for _, o in items) / len(items),
                }
            )
    return out


def prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    """Precision/recall/F1 from counts (an empty denominator scores 1.0: nothing to get wrong)."""
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    return {"precision": p, "recall": r, "f1": 2 * p * r / (p + r) if p + r else 0.0}
