"""
rotation_signal.py
==================
Equal-weight (RSP) vs cap-weight (SPY) rotation signal for the pre-market
market-context block of live-market-scanner.

The idea (from the top-down swing framework): the cap-weighted S&P is dominated
by a handful of mega-caps. When cap-weight looks weak but equal-weight holds up,
that is usually *rotation and profit-taking* inside the market rather than broad
selling *out* of it. Comparing the two directly surfaces that.

Two axes:
  1. Market direction   -> is SPY in an uptrend? (price vs 50-day SMA + 20-day return)
  2. Breadth direction  -> is the RSP/SPY ratio rising? (equal-weight outperforming)

Four regimes:
  SPY up   + ratio up    -> BROAD ADVANCE   (healthy; best backdrop for long swings)
  SPY up   + ratio down  -> NARROW ADVANCE  (mega-cap led; fragile, be selective)
  SPY down + ratio up    -> ROTATION        (money rotating, not fleeing; find RS names)
  SPY down + ratio down  -> BROAD DECLINE    (risk-off; favor caution / short setups)

Designed to fail soft: any fetch/compute error returns a NEUTRAL/UNKNOWN result
with an error note instead of raising, so it can never take down the email job.

Dependencies: yfinance, pandas (already in the scanner environment).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

try:
    import yfinance as yf
    _HAVE_YF = True
except Exception:  # pragma: no cover - env without yfinance
    _HAVE_YF = False


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CAP_WEIGHT = "SPY"          # cap-weighted S&P 500
EQUAL_WEIGHT = "RSP"        # equal-weighted S&P 500
MOMENTUM_LOOKBACK = 20      # trading days for the "recent" return / ratio change
FETCH_PERIOD = "6mo"        # enough bars for a clean 50-day SMA with margin
SMA_FAST = 20
SMA_SLOW = 50
# Deadband: RSP/SPY 20-day change smaller than this (in %) is treated as flat,
# so day-to-day noise doesn't flip the regime. Tune if it feels too sticky/jumpy.
BREADTH_DEADBAND_PCT = 0.25


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class RotationSignal:
    regime: str = "UNKNOWN"          # BROAD ADVANCE / NARROW ADVANCE / ROTATION / BROAD DECLINE / UNKNOWN
    bias: str = "NEUTRAL"            # BULLISH / NEUTRAL-BULLISH / NEUTRAL / CAUTION / BEARISH
    interpretation: str = ""         # one-line plain-English read
    spy_uptrend: Optional[bool] = None
    breadth_rising: Optional[bool] = None
    spy_return_pct: Optional[float] = None      # 20-day % change, cap-weight
    rsp_return_pct: Optional[float] = None       # 20-day % change, equal-weight
    ratio_change_pct: Optional[float] = None     # 20-day % change of RSP/SPY (breadth)
    ratio_vs_sma_pct: Optional[float] = None     # ratio vs its 20-day SMA, confirmation
    spy_vs_sma50_pct: Optional[float] = None     # SPY price vs its 50-day SMA
    asof: str = ""
    error: Optional[str] = None
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Data fetch (isolated so it's easy to mock/swap)
# ---------------------------------------------------------------------------

def fetch_closes(tickers, period: str = FETCH_PERIOD) -> pd.DataFrame:
    """Return a DataFrame of daily closes indexed by date, one column per ticker.

    Raises on failure; callers wrap this in try/except.
    """
    if not _HAVE_YF:
        raise RuntimeError("yfinance not available in this environment")

    data = yf.download(
        tickers,
        period=period,
        interval="1d",
        auto_adjust=True,
        progress=False,
        group_by="column",
    )
    # yfinance returns a MultiIndex when multiple tickers are requested.
    if isinstance(data.columns, pd.MultiIndex):
        closes = data["Close"].copy()
    else:  # single ticker fallback
        closes = data[["Close"]].copy()
        closes.columns = tickers if isinstance(tickers, list) else [tickers]

    closes = closes.dropna(how="all")
    if closes.empty:
        raise ValueError("no price data returned")
    return closes


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def _pct_change(series: pd.Series, lookback: int) -> float:
    s = series.dropna()
    if len(s) <= lookback:
        raise ValueError(f"not enough data ({len(s)} rows) for lookback {lookback}")
    return (s.iloc[-1] / s.iloc[-1 - lookback] - 1.0) * 100.0


def classify(spy_uptrend: bool, ratio_change_pct: float,
             deadband: float = BREADTH_DEADBAND_PCT) -> tuple[str, str, str]:
    """Map the two axes to (regime, bias, interpretation)."""
    breadth_rising = ratio_change_pct > deadband
    breadth_falling = ratio_change_pct < -deadband
    # inside the deadband -> flat breadth, treated as "not rising"

    if spy_uptrend and breadth_rising:
        return (
            "BROAD ADVANCE",
            "BULLISH",
            "Cap-weight rising and the average stock is keeping up — broad, "
            "healthy participation. Best backdrop for long swings.",
        )
    if spy_uptrend and not breadth_rising:
        return (
            "NARROW ADVANCE",
            "NEUTRAL-BULLISH",
            "Index up but carried by mega-caps; breadth is lagging. Advance is "
            "fragile — be selective and watch for a breadth reversal.",
        )
    if (not spy_uptrend) and breadth_rising:
        return (
            "ROTATION",
            "NEUTRAL",
            "Cap-weight soft but the average stock is holding up — money is "
            "rotating, not fleeing. Hunt for relative-strength sectors/names.",
        )
    if (not spy_uptrend) and breadth_falling:
        return (
            "BROAD DECLINE",
            "BEARISH",
            "Both cap-weight and breadth falling — broad risk-off. Favor caution; "
            "long setups low-odds, short setups favored.",
        )
    # SPY down, breadth flat (inside deadband)
    return (
        "BROAD DECLINE",
        "CAUTION",
        "Cap-weight soft and breadth flat — no rotation cushion. Stay cautious "
        "until breadth turns up or the index reclaims trend.",
    )


def compute_rotation_signal(period: str = FETCH_PERIOD,
                            lookback: int = MOMENTUM_LOOKBACK,
                            closes: Optional[pd.DataFrame] = None) -> RotationSignal:
    """Compute the rotation signal.

    Pass `closes` (a DataFrame with SPY and RSP columns) to bypass the network
    fetch — used for testing. Otherwise it fetches live via yfinance.
    """
    sig = RotationSignal(asof=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    try:
        if closes is None:
            closes = fetch_closes([CAP_WEIGHT, EQUAL_WEIGHT], period=period)

        for col in (CAP_WEIGHT, EQUAL_WEIGHT):
            if col not in closes.columns:
                raise ValueError(f"missing column {col} in price data")

        spy = closes[CAP_WEIGHT].dropna()
        rsp = closes[EQUAL_WEIGHT].dropna()
        common = spy.index.intersection(rsp.index)
        spy, rsp = spy.loc[common], rsp.loc[common]

        ratio = rsp / spy

        # Axis 1: market direction
        spy_ret = _pct_change(spy, lookback)
        sma50 = spy.rolling(SMA_SLOW).mean().iloc[-1]
        spy_vs_sma50 = (spy.iloc[-1] / sma50 - 1.0) * 100.0
        # uptrend if price above 50-SMA AND recent return positive
        spy_uptrend = (spy.iloc[-1] > sma50) and (spy_ret > 0)

        # Axis 2: breadth direction
        rsp_ret = _pct_change(rsp, lookback)
        ratio_change = _pct_change(ratio, lookback)
        ratio_sma = ratio.rolling(SMA_FAST).mean().iloc[-1]
        ratio_vs_sma = (ratio.iloc[-1] / ratio_sma - 1.0) * 100.0

        regime, bias, interp = classify(spy_uptrend, ratio_change)

        # Confirmation note: does the ratio-vs-SMA agree with the ratio-change read?
        if (ratio_change > BREADTH_DEADBAND_PCT) != (ratio_vs_sma > 0):
            sig.notes.append(
                "Breadth signals mixed: 20-day ratio change and ratio-vs-SMA "
                "disagree — treat rotation read as tentative."
            )

        sig.regime = regime
        sig.bias = bias
        sig.interpretation = interp
        sig.spy_uptrend = bool(spy_uptrend)
        sig.breadth_rising = bool(ratio_change > BREADTH_DEADBAND_PCT)
        sig.spy_return_pct = round(spy_ret, 2)
        sig.rsp_return_pct = round(rsp_ret, 2)
        sig.ratio_change_pct = round(ratio_change, 2)
        sig.ratio_vs_sma_pct = round(ratio_vs_sma, 2)
        sig.spy_vs_sma50_pct = round(spy_vs_sma50, 2)

    except Exception as exc:  # fail soft
        sig.error = f"{type(exc).__name__}: {exc}"
        sig.interpretation = "Rotation signal unavailable (see error)."
        sig.notes.append("Signal skipped; scanner continued normally.")

    return sig


# ---------------------------------------------------------------------------
# Email formatting
# ---------------------------------------------------------------------------

def format_for_email(sig: RotationSignal) -> str:
    """Plain-text block for the pre-market email market-context section."""
    if sig.error:
        return (
            "MARKET BREADTH (RSP vs SPY)\n"
            f"  Unavailable — {sig.error}\n"
        )

    lines = [
        "MARKET BREADTH (RSP vs SPY)",
        f"  Regime : {sig.regime}   |   Bias: {sig.bias}",
        f"  {sig.interpretation}",
        f"  SPY 20d: {sig.spy_return_pct:+.2f}%   "
        f"RSP 20d: {sig.rsp_return_pct:+.2f}%   "
        f"Breadth (RSP/SPY 20d): {sig.ratio_change_pct:+.2f}%",
        f"  SPY vs 50-SMA: {sig.spy_vs_sma50_pct:+.2f}%   "
        f"Ratio vs 20-SMA: {sig.ratio_vs_sma_pct:+.2f}%",
    ]
    for note in sig.notes:
        lines.append(f"  ! {note}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    s = compute_rotation_signal()
    print(format_for_email(s))
    print("\nraw:", s.to_dict())