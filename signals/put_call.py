"""Signal 5 — Put/Call Sentiment proxy.

VIX 20-day rate of change as a sentiment proxy.
  Rapidly rising VIX  = fear   = low score.
  ROC -30% -> 100  (fear collapsing, complacency returning)
  ROC +50% ->   0  (fear spiking)
Linear, clamped.

Note: a true put/call ratio would come from CBOE. VIX 20-day ROC is the
spec's chosen proxy because it captures the same "fear acceleration" signal
without a paid data feed.
"""
from __future__ import annotations

from .data_utils import VIX, get_close, linear_map
from .vix_level import SignalResult


def compute() -> SignalResult:
    vix = get_close(VIX, period="3mo")
    if len(vix) < 21:
        raise RuntimeError("Put/Call: not enough VIX history for 20-day ROC")

    current = float(vix.iloc[-1])
    twenty_ago = float(vix.iloc[-21])
    roc_pct = 100.0 * (current - twenty_ago) / twenty_ago

    # ROC -30% -> 100, ROC +50% -> 0
    score = linear_map(roc_pct, x_lo=-30, x_hi=50, y_lo=100, y_hi=0)

    if roc_pct > 20:
        regime = "fear spiking"
    elif roc_pct < -15:
        regime = "fear collapsing"
    else:
        regime = "neutral"

    detail = (
        f"VIX 20d ROC = {roc_pct:+.1f}% ({regime}). "
        f"VIX now {current:.2f} vs {twenty_ago:.2f} 20d ago."
    )
    return SignalResult(score=score, value=roc_pct, detail=detail)
