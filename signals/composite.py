"""Composite scoring and deployment zone classification.

Weighted blend:
  VIX Level       0.25
  Term Structure  0.20
  Breadth         0.20
  Credit          0.15
  Put/Call        0.10
  Crowding        0.10

Zones:
  70-100  FULL DEPLOY   (100% sizing)
  40-69   REDUCED       (60% sizing, higher bar for new positions)
   0-39   DEFENSIVE     (25% sizing, no new longs, scanner disabled)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from . import (
    breadth,
    credit_spreads,
    crowding,
    put_call,
    vix_level,
    vix_term_structure,
)
from .vix_level import SignalResult


WEIGHTS: dict[str, float] = {
    "VIX Level":      0.25,
    "Term Structure": 0.20,
    "Breadth":        0.20,
    "Credit":         0.15,
    "Put/Call":       0.10,
    "Crowding":       0.10,
}

SIGNAL_FNS: dict[str, Callable[[], SignalResult]] = {
    "VIX Level":      vix_level.compute,
    "Term Structure": vix_term_structure.compute,
    "Breadth":        breadth.compute,
    "Credit":         credit_spreads.compute,
    "Put/Call":       put_call.compute,
    "Crowding":       crowding.compute,
}


@dataclass
class Zone:
    name: str
    sizing_pct: int
    instruction: str
    color: str   # hex for the dashboard


FULL = Zone("FULL DEPLOY", 100,
            "100% sizing",
            "#22c55e")     # green
REDUCED = Zone("REDUCED", 60,
               "60% sizing, higher bar for new positions",
               "#eab308")  # yellow
DEFENSIVE = Zone("DEFENSIVE", 25,
                 "25% sizing, no new longs, scanner disabled",
                 "#ef4444") # red


def classify(score: float) -> Zone:
    if score >= 70:
        return FULL
    if score >= 40:
        return REDUCED
    return DEFENSIVE


@dataclass
class CompositeResult:
    score: float
    zone: Zone
    signals: dict[str, SignalResult] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def compute(progress_cb: Callable[[str], None] | None = None) -> CompositeResult:
    """Run all six signals, blend, and classify.

    If a signal fails, we log the error and renormalize remaining weights so
    one broken data feed doesn't zero out the whole score.
    """
    results: dict[str, SignalResult] = {}
    errors: dict[str, str] = {}

    for name, fn in SIGNAL_FNS.items():
        if progress_cb:
            progress_cb(name)
        try:
            results[name] = fn()
        except Exception as e:
            errors[name] = f"{type(e).__name__}: {e}"

    if not results:
        raise RuntimeError("All signals failed — check data connection.")

    # Renormalize weights over successful signals
    total_weight = sum(WEIGHTS[n] for n in results)
    composite = sum(WEIGHTS[n] * r.score for n, r in results.items()) / total_weight

    return CompositeResult(
        score=composite,
        zone=classify(composite),
        signals=results,
        errors=errors,
    )
