"""Universe loading and bulk OHLCV download for the scanner."""
from __future__ import annotations

import pandas as pd
import yfinance as yf

from signals.data_utils import SPY, get_sp500_tickers

from . import config


def get_universe() -> list[str]:
    """Return the ticker list for the configured universe."""
    if config.UNIVERSE == "SP500":
        return list(get_sp500_tickers())
    raise ValueError(f"Unknown universe: {config.UNIVERSE}")


def download_ohlcv(tickers: list[str], period: str = config.PRICE_LOOKBACK
                   ) -> dict[str, pd.DataFrame]:
    """Batch-download OHLCV for the universe.

    Returns a dict ticker -> DataFrame with columns [Open, High, Low, Close, Volume].
    Tickers with insufficient data are dropped silently.
    """
    data = yf.download(
        tickers,
        period=period,
        interval="1d",
        progress=False,
        auto_adjust=True,
        group_by="ticker",
        threads=True,
    )

    out: dict[str, pd.DataFrame] = {}
    for t in tickers:
        try:
            df = data[t][["Open", "High", "Low", "Close", "Volume"]].dropna()
            if len(df) >= 200:        # need enough history for all factors
                out[t] = df
        except (KeyError, TypeError):
            continue
    return out


def get_spy(period: str = config.PRICE_LOOKBACK) -> pd.Series:
    """SPY close series — used as the RS benchmark."""
    df = yf.download(SPY, period=period, interval="1d",
                     progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df["Close"].dropna()
