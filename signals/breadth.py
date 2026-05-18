"""Signal 3 — Market Breadth.

% of S&P 500 stocks above 200-day SMA.
  80% -> 100, 30% -> 0 (linear, clamped).
Catches narrow rallies driven by a few mega-caps while everything else declines.

NOTE: This is the slowest signal (~500 ticker downloads). We use a batched
yfinance download and cache aggressively at the Streamlit layer.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import yfinance as yf

from .data_utils import get_sp500_tickers, linear_map
from .vix_level import SignalResult


def _percent_above_sma200() -> tuple[float, int, int]:
    tickers = list(get_sp500_tickers())
    # Batch download — yfinance accepts a space-separated string.
    data = yf.download(
        tickers,
        period="14mo",       # need ~200 trading days = ~10 months, give buffer
        interval="1d",
        progress=False,
        auto_adjust=True,
        group_by="ticker",
        threads=True,
    )

    above = 0
    counted = 0
    for t in tickers:
        try:
            close = data[t]["Close"].dropna()
        except (KeyError, TypeError):
            continue
        if len(close) < 200:
            continue
        sma200 = close.rolling(200).mean().iloc[-1]
        last = close.iloc[-1]
        if np.isnan(sma200) or np.isnan(last):
            continue
        counted += 1
        if last > sma200:
            above += 1

    if counted == 0:
        raise RuntimeError("Breadth: no tickers had enough data")
    pct = 100.0 * above / counted
    return pct, above, counted


def compute() -> SignalResult:
    pct, above, counted = _percent_above_sma200()
    score = linear_map(pct, x_lo=30, x_hi=80, y_lo=0, y_hi=100)
    detail = (
        f"{pct:.1f}% of S&P 500 above 200-day SMA "
        f"({above}/{counted} names with sufficient history)."
    )
    return SignalResult(score=score, value=pct, detail=detail)
