"""Signal 2 — VIX Term Structure.

Ratio = front-month VIX / VIX3M.
  Below 1.0 = contango = calm   -> high score
  Above 1.0 = backwardation = stress -> low score
Mapping: 0.85 -> 100, 1.15 -> 0 (linear, clamped).
"""
from __future__ import annotations

from .data_utils import VIX, VIX3M, get_close, linear_map
from .vix_level import SignalResult


def compute() -> SignalResult:
    vix = get_close(VIX, period="6mo")
    vix3m = get_close(VIX3M, period="6mo")

    # Align on shared dates and take the latest available bar.
    df = vix.to_frame("vix").join(vix3m.to_frame("vix3m"), how="inner").dropna()
    latest = df.iloc[-1]
    ratio = float(latest["vix"] / latest["vix3m"])

    score = linear_map(ratio, x_lo=0.85, x_hi=1.15, y_lo=100, y_hi=0)

    if ratio < 1.0:
        regime = "contango (calm)"
    elif ratio > 1.0:
        regime = "backwardation (stress)"
    else:
        regime = "flat"

    detail = (
        f"VIX/VIX3M = {ratio:.3f} ({regime}). "
        f"VIX {latest['vix']:.2f}, VIX3M {latest['vix3m']:.2f}."
    )
    return SignalResult(score=score, value=ratio, detail=detail)
