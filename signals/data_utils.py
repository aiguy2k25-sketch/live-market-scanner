"""Shared data-fetching utilities for all signal modules.

We centralize yfinance access here so:
  - Caching is consistent (Streamlit will wrap these).
  - Tickers and lookbacks are defined in one place.
  - Tests / swaps to a different data source touch one file.
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache

import pandas as pd
import yfinance as yf


# --- Tickers ----------------------------------------------------------------
VIX = "^VIX"          # CBOE Volatility Index (30-day)
VIX3M = "^VIX3M"      # 3-month VIX
HYG = "HYG"           # iShares iBoxx $ High Yield Corp Bond ETF
TLT = "TLT"           # iShares 20+ Year Treasury Bond ETF
SPY = "SPY"           # S&P 500 ETF (proxy for index)


def _download(ticker: str, period: str = "2y") -> pd.DataFrame:
    """Thin wrapper over yfinance with sensible defaults."""
    df = yf.download(
        ticker,
        period=period,
        interval="1d",
        progress=False,
        auto_adjust=True,
    )
    if df.empty:
        raise RuntimeError(f"No data returned for {ticker}")
    # yfinance sometimes returns a MultiIndex with single ticker; flatten it.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def get_close(ticker: str, period: str = "2y") -> pd.Series:
    """Return adjusted close series for a single ticker."""
    df = _download(ticker, period=period)
    return df["Close"].dropna()


def get_sp500_tickers() -> list[str]:
    """Scrape current S&P 500 constituents from Wikipedia.

    This isn't perfect (Wikipedia lags rebalances by a few days), but it's
    free and good enough for a breadth signal. Cached per-process.
    """
    return _cached_sp500()


@lru_cache(maxsize=1)
def _cached_sp500() -> tuple[str, ...]:
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    tables = pd.read_html(url)
    tickers = tables[0]["Symbol"].tolist()
    # Yahoo uses '-' where Wikipedia uses '.' (BRK.B -> BRK-B)
    tickers = [t.replace(".", "-") for t in tickers]
    return tuple(tickers)


def percentile_rank(series: pd.Series, value: float) -> float:
    """Return the percentile (0-100) of `value` within `series`."""
    series = series.dropna()
    if series.empty:
        return 50.0
    return float((series <= value).mean() * 100)


def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def linear_map(x: float, x_lo: float, x_hi: float,
               y_lo: float = 0.0, y_hi: float = 100.0) -> float:
    """Map x from [x_lo, x_hi] linearly onto [y_lo, y_hi], clamped."""
    if x_hi == x_lo:
        return (y_lo + y_hi) / 2
    t = (x - x_lo) / (x_hi - x_lo)
    return clamp(y_lo + t * (y_hi - y_lo), min(y_lo, y_hi), max(y_lo, y_hi))


def today() -> dt.date:
    return dt.date.today()
