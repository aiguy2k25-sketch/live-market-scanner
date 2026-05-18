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
    """Scrape current S&P 500 constituents.

    Tries the GitHub CSV mirror first (reliable from data center IPs like
    GitHub Actions), then Wikipedia as a fallback. Cached per-process.
    """
    return list(_cached_sp500())


@lru_cache(maxsize=1)
def _cached_sp500() -> tuple[str, ...]:
    """Fetch S&P 500 constituents.

    Strategy:
      1. Try the datasets/s-and-p-500-companies GitHub CSV (reliable from
         any IP including GitHub Actions runners).
      2. Fall back to Wikipedia with a UA header.
    """
    import io
    import urllib.request

    headers = {"User-Agent": "Mozilla/5.0 (compatible; daily-quant/1.0)"}

    # Attempt 1: GitHub-hosted CSV (most reliable for CI environments)
    try:
        url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read().decode("utf-8")
        df = pd.read_csv(io.StringIO(data))
        tickers = [t.replace(".", "-") for t in df["Symbol"].tolist()]
        if len(tickers) >= 450:
            return tuple(tickers)
    except Exception:
        pass

    # Attempt 2: Wikipedia with proper headers
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8")
        tables = pd.read_html(io.StringIO(html))
        tickers = [t.replace(".", "-") for t in tables[0]["Symbol"].tolist()]
        if len(tickers) >= 450:
            return tuple(tickers)
    except Exception:
        pass

    raise RuntimeError(
        "Could not fetch S&P 500 constituent list from any source."
    )


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
