"""Signal 4 — Credit Spreads.

HYG vs TLT spread proxy, z-score against 1-year history.
We use the ratio HYG/TLT as the proxy:
  - When credit is calm, HY outperforms long Treasuries -> ratio rises.
  - When credit is stressed, HY underperforms -> ratio falls.

So a HIGH z-score on HYG/TLT = tight spreads = good.
We want the spec's intuition: tight (z = +2 in our framing) -> 100,
wide (z = -2) -> 0.
"""
from __future__ import annotations

from .data_utils import HYG, TLT, get_close, linear_map
from .vix_level import SignalResult


def compute() -> SignalResult:
    hyg = get_close(HYG, period="2y")
    tlt = get_close(TLT, period="2y")
    df = hyg.to_frame("hyg").join(tlt.to_frame("tlt"), how="inner").dropna()
    df["ratio"] = df["hyg"] / df["tlt"]

    # 1-year z-score of the ratio.
    window = df["ratio"].iloc[-252:]
    mean = window.mean()
    std = window.std(ddof=0)
    if std == 0:
        z = 0.0
    else:
        z = float((df["ratio"].iloc[-1] - mean) / std)

    # High z (HY strong vs TLT) = tight spreads = good.
    score = linear_map(z, x_lo=-2, x_hi=2, y_lo=0, y_hi=100)

    if z > 0.5:
        regime = "tight (risk-on)"
    elif z < -0.5:
        regime = "wide (risk-off)"
    else:
        regime = "neutral"

    detail = (
        f"HYG/TLT ratio z-score = {z:+.2f} over 1y ({regime}). "
        f"Latest ratio {df['ratio'].iloc[-1]:.3f}, "
        f"1y mean {mean:.3f} ± {std:.3f}."
    )
    return SignalResult(score=score, value=z, detail=detail)
