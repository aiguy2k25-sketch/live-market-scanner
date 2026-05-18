"""Signal 1 — VIX Level.

Percentile-rank current VIX against trailing 1 year. Low VIX = high score.
Bonus +5 if VIX < 15. Penalty -10 if VIX > 30.
"""
from __future__ import annotations

from dataclasses import dataclass

from .data_utils import VIX, clamp, get_close, percentile_rank


@dataclass
class SignalResult:
    score: float        # 0-100
    value: float        # raw signal value (here, the VIX level)
    detail: str         # human-readable explanation


def compute() -> SignalResult:
    vix = get_close(VIX, period="1y")
    current = float(vix.iloc[-1])

    # Percentile rank within trailing 1y. Low VIX -> high percentile of "calm" ->
    # we want LOW vix to score HIGH, so we invert.
    pct = percentile_rank(vix, current)   # higher pct = current vix is high
    base = 100.0 - pct                    # invert: low vix -> high score

    bonus = 5.0 if current < 15 else 0.0
    penalty = -10.0 if current > 30 else 0.0
    score = clamp(base + bonus + penalty)

    detail = (
        f"VIX = {current:.2f} "
        f"({pct:.0f}th pct of trailing 1y). "
        f"Base {base:.0f}"
        + (f"  +5 bonus (<15)" if bonus else "")
        + (f"  -10 penalty (>30)" if penalty else "")
    )
    return SignalResult(score=score, value=current, detail=detail)
