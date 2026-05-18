"""Five-factor scoring engine.

Each factor produces a raw value per ticker, then we percentile-rank
the raw values across the universe to produce a 0-100 score.
Composite = equal weight of the 5 factor scores.

Factors:
  1. Momentum Trend Strength (10 EMA gap to 50 EMA + 3m return)
  2. Volume Surge       (5d avg / 20d avg)
  3. Relative Strength  (20d stock ret - 20d SPY ret)
  4. 52-Week High Proximity (price / 52w high; >0.95 best)
  5. Institutional Ownership (heldPercentInstitutions — smart money proxy)
"""
from __future__ import annotations

import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import numpy as np
import pandas as pd
import yfinance as yf

from . import config


def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def _factor_momentum_trend(ohlcv: pd.DataFrame) -> float | None:
    """Continuous trend strength: EMA gap % + 3-month return %."""
    close = ohlcv["Close"]
    if len(close) < max(config.EMA_SLOW, 63):
        return None
    fast = _ema(close, config.EMA_FAST)
    slow = _ema(close, config.EMA_SLOW)
    gap_pct = float((fast.iloc[-1] - slow.iloc[-1]) / slow.iloc[-1]) * 100
    ret_3m = float(close.iloc[-1] / close.iloc[-63] - 1.0) * 100
    return gap_pct + ret_3m


def _factor_volume_surge(ohlcv: pd.DataFrame) -> float | None:
    vol = ohlcv["Volume"]
    if len(vol) < config.VOL_LONG:
        return None
    short_avg = vol.iloc[-config.VOL_SHORT:].mean()
    long_avg = vol.iloc[-config.VOL_LONG:].mean()
    if long_avg == 0:
        return None
    return float(short_avg / long_avg)


def _factor_relative_strength(ohlcv: pd.DataFrame, spy: pd.Series) -> float | None:
    close = ohlcv["Close"]
    if len(close) < config.RS_LOOKBACK + 1:
        return None
    stock_ret = float(close.iloc[-1] / close.iloc[-config.RS_LOOKBACK - 1] - 1.0)
    spy_aligned = spy.reindex(close.index).ffill()
    if len(spy_aligned.dropna()) < config.RS_LOOKBACK + 1:
        return None
    spy_ret = float(spy_aligned.iloc[-1] / spy_aligned.iloc[-config.RS_LOOKBACK - 1] - 1.0)
    return (stock_ret - spy_ret) * 100


def _factor_52w_high_proximity(ohlcv: pd.DataFrame) -> float | None:
    close = ohlcv["Close"]
    if len(close) < config.HIGH_LOOKBACK:
        window = close
    else:
        window = close.iloc[-config.HIGH_LOOKBACK:]
    high = float(window.max())
    if high == 0:
        return None
    return float(close.iloc[-1] / high)


def _institutional_ownership(tickers: list[str]) -> dict[str, float]:
    """Smart-money proxy: heldPercentInstitutions per ticker."""
    results: dict[str, float] = {}

    def fetch(t: str) -> tuple[str, float | None]:
        try:
            info = yf.Ticker(t).info
            v = info.get("heldPercentInstitutions")
            if v is None or (isinstance(v, float) and np.isnan(v)):
                return t, None
            return t, float(v)
        except Exception:
            return t, None

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with ThreadPoolExecutor(max_workers=10) as ex:
            futures = [ex.submit(fetch, t) for t in tickers]
            for f in as_completed(futures):
                t, v = f.result()
                if v is not None:
                    results[t] = v
    return results


def _pct_rank(series: pd.Series, ascending: bool = True) -> pd.Series:
    if not ascending:
        series = -series
    return series.rank(pct=True, na_option="bottom") * 100


@dataclass
class ScanResult:
    df: pd.DataFrame
    universe_size: int
    coverage: dict[str, int]


def run_scan(
    ohlcv_by_ticker: dict[str, pd.DataFrame],
    spy: pd.Series,
    progress_cb=None,
) -> ScanResult:
    tickers = list(ohlcv_by_ticker.keys())

    rows = []
    for t in tickers:
        ohlcv = ohlcv_by_ticker[t]
        row = {
            "ticker": t,
            "price": float(ohlcv["Close"].iloc[-1]),
            "f1_trend_strength": _factor_momentum_trend(ohlcv),
            "f2_vol_surge_ratio": _factor_volume_surge(ohlcv),
            "f3_rs_vs_spy": _factor_relative_strength(ohlcv, spy),
            "f4_pct_of_52w_high": _factor_52w_high_proximity(ohlcv),
        }
        rows.append(row)

    if progress_cb:
        progress_cb("Fetching institutional ownership snapshots…")
    inst_data = _institutional_ownership(tickers)
    for row in rows:
        row["f5_inst_ownership"] = inst_data.get(row["ticker"])

    df = pd.DataFrame(rows).set_index("ticker")

    df["score_1_momentum"]  = _pct_rank(df["f1_trend_strength"],   ascending=True)
    df["score_2_vol_surge"] = _pct_rank(df["f2_vol_surge_ratio"],  ascending=True)
    df["score_3_rs"]        = _pct_rank(df["f3_rs_vs_spy"],        ascending=True)
    df["score_4_high_prox"] = _pct_rank(df["f4_pct_of_52w_high"],  ascending=True)
    df["score_5_inst"]      = _pct_rank(df["f5_inst_ownership"],   ascending=True)

    score_cols = ["score_1_momentum", "score_2_vol_surge", "score_3_rs",
                  "score_4_high_prox", "score_5_inst"]
    df["composite"] = df[score_cols].mean(axis=1)

    coverage = {
        "Momentum trend strength": int(df["f1_trend_strength"].notna().sum()),
        "Volume surge": int(df["f2_vol_surge_ratio"].notna().sum()),
        "Relative strength": int(df["f3_rs_vs_spy"].notna().sum()),
        "52w high proximity": int(df["f4_pct_of_52w_high"].notna().sum()),
        "Institutional ownership": int(df["f5_inst_ownership"].notna().sum()),
    }

    df = df.sort_values("composite", ascending=False)
    return ScanResult(df=df, universe_size=len(tickers), coverage=coverage)
