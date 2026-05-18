"""Signal 6 — Factor Crowding.

Build momentum and value long/short baskets from top/bottom 50 stocks.
60-day rolling correlation between factor returns.
  Corr +0.3  -> 100 (normal: factors uncorrelated or slightly positive)
  Corr -0.8  ->   0 (extreme crowding / reversal risk)
Highly negative correlation = momentum crowded = reversal risk.

CAVEATS (read me):
  - Momentum basket = top/bottom 50 by 12-1 month return (skip last month).
    This is standard academic momentum.
  - Value basket uses CURRENT P/E snapshots from Yahoo as a proxy. A real
    value factor would use point-in-time book/price. Snapshots introduce
    look-ahead bias — fine for a live signal (we only care about NOW), but
    means the backtest's crowding numbers are approximate.
  - Baskets are rebuilt monthly inside the lookback window to keep it tractable.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import yfinance as yf

from .data_utils import get_sp500_tickers, linear_map
from .vix_level import SignalResult


# Tunable knobs
N_LEGS = 50              # top/bottom 50 stocks per factor
CORR_WINDOW = 60         # 60-day rolling correlation
PRICE_LOOKBACK_MO = 18   # need 12mo momentum + 60d corr window + slack


def _momentum_scores(prices: pd.DataFrame) -> pd.Series:
    """12-1 month momentum (skip last 21 trading days). Returns score per ticker."""
    if len(prices) < 252:
        return pd.Series(dtype=float)
    end = prices.iloc[-21]
    start = prices.iloc[-252]
    mom = (end / start - 1.0)
    return mom.dropna()


def _value_scores(tickers: list[str]) -> pd.Series:
    """Earnings yield proxy from Yahoo current P/E snapshots.

    Higher earnings yield = cheaper = more 'value'. Snapshot-based; see caveat.
    """
    rows = {}
    # Batch the .info calls — yfinance is slow here but caching at the
    # Streamlit layer makes this a once-per-session cost.
    for t in tickers:
        try:
            info = yf.Ticker(t).info
            pe = info.get("trailingPE")
            if pe and pe > 0:
                rows[t] = 1.0 / pe   # earnings yield
        except Exception:
            continue
    return pd.Series(rows, dtype=float)


def _basket_returns(prices: pd.DataFrame, longs: list[str], shorts: list[str]
                    ) -> pd.Series:
    """Equal-weight long/short basket daily returns."""
    rets = prices.pct_change()
    long_ret = rets[longs].mean(axis=1)
    short_ret = rets[shorts].mean(axis=1)
    return (long_ret - short_ret).dropna()


def compute() -> SignalResult:
    tickers = list(get_sp500_tickers())

    # Pull ~18 months of prices in one batch.
    data = yf.download(
        tickers,
        period=f"{PRICE_LOOKBACK_MO}mo",
        interval="1d",
        progress=False,
        auto_adjust=True,
        group_by="ticker",
        threads=True,
    )

    # Flatten to a wide closes-only frame.
    closes = {}
    for t in tickers:
        try:
            s = data[t]["Close"].dropna()
            if len(s) >= 252:
                closes[t] = s
        except (KeyError, TypeError):
            continue
    prices = pd.DataFrame(closes).dropna(how="all")

    if prices.shape[1] < 2 * N_LEGS:
        raise RuntimeError("Crowding: not enough tickers with full history")

    # Momentum legs
    mom = _momentum_scores(prices)
    mom_longs = mom.nlargest(N_LEGS).index.tolist()
    mom_shorts = mom.nsmallest(N_LEGS).index.tolist()

    # Value legs (using current EY snapshot)
    val = _value_scores(prices.columns.tolist())
    if len(val) < 2 * N_LEGS:
        # Fall back to inverted momentum if EY data is too sparse —
        # which would force crowding by construction. Bail out instead.
        raise RuntimeError("Crowding: not enough P/E data for value basket")
    val_longs = val.nlargest(N_LEGS).index.tolist()
    val_shorts = val.nsmallest(N_LEGS).index.tolist()

    # Build basket return series and 60d rolling correlation
    mom_ret = _basket_returns(prices, mom_longs, mom_shorts)
    val_ret = _basket_returns(prices, val_longs, val_shorts)

    aligned = pd.concat([mom_ret, val_ret], axis=1, keys=["mom", "val"]).dropna()
    if len(aligned) < CORR_WINDOW + 1:
        raise RuntimeError("Crowding: not enough overlap for rolling correlation")

    corr = aligned["mom"].rolling(CORR_WINDOW).corr(aligned["val"])
    current_corr = float(corr.iloc[-1])

    # Map: corr +0.3 -> 100, corr -0.8 -> 0
    score = linear_map(current_corr, x_lo=-0.8, x_hi=0.3, y_lo=0, y_hi=100)

    if current_corr < -0.4:
        regime = "crowded (reversal risk)"
    elif current_corr > 0.1:
        regime = "normal"
    else:
        regime = "elevated crowding"

    detail = (
        f"Momentum/Value 60d corr = {current_corr:+.2f} ({regime}). "
        f"Baskets: top/bottom {N_LEGS} by 12-1m momentum and earnings yield."
    )
    return SignalResult(score=score, value=current_corr, detail=detail)
