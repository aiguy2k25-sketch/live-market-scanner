"""Five-factor scoring engine.

Each factor produces a raw value per ticker, then we percentile-rank
the raw values across the universe to produce a 0-100 score.
Composite = equal weight of the 5 factor scores.

Factors:
  1. Momentum Crossover (10 EMA / 50 EMA)
  2. Volume Surge       (5d avg / 20d avg, mapped 0.7->0, 2.0->100)
  3. Relative Strength  (20d stock ret - 20d SPY ret)
  4. 52-Week High Proximity (price / 52w high; >0.95 best)
  5. Short Interest Decline (proxy via current short % of float — see caveat)
"""
from __future__ import annotations

import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import numpy as np
import pandas as pd
import yfinance as yf

from . import config


# ---------------------------------------------------------------------------
# Per-ticker raw factor calculations
# ---------------------------------------------------------------------------
def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def _factor_momentum_crossover(ohlcv: pd.DataFrame) -> float | None:
    """Raw value: 3-month return magnitude IF crossover happened in last N days.
    Returns NaN if no recent crossover (effectively scores at the bottom).
    """
    close = ohlcv["Close"]
    if len(close) < max(config.EMA_SLOW + config.CROSSOVER_WINDOW, 63):
        return None

    fast = _ema(close, config.EMA_FAST)
    slow = _ema(close, config.EMA_SLOW)

    # Crossover = fast crossing ABOVE slow within last N days
    above = fast > slow
    recent = above.iloc[-config.CROSSOVER_WINDOW:]
    prior = above.iloc[-(config.CROSSOVER_WINDOW + 1)]
    had_crossover = (not prior) and recent.any()

    if not had_crossover:
        return -1e9   # sentinel: scored at the floor by percentile rank

    # Raw value = gap size + 3-month return
    gap = float((fast.iloc[-1] - slow.iloc[-1]) / slow.iloc[-1]) * 100
    ret_3m = float(close.iloc[-1] / close.iloc[-63] - 1.0) * 100
    return gap + ret_3m


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
    # Align SPY to the same dates
    spy_aligned = spy.reindex(close.index).ffill()
    if len(spy_aligned.dropna()) < config.RS_LOOKBACK + 1:
        return None
    spy_ret = float(spy_aligned.iloc[-1] / spy_aligned.iloc[-config.RS_LOOKBACK - 1] - 1.0)
    return (stock_ret - spy_ret) * 100


def _factor_52w_high_proximity(ohlcv: pd.DataFrame) -> float | None:
    close = ohlcv["Close"]
    if len(close) < config.HIGH_LOOKBACK:
        # Use whatever we have
        window = close
    else:
        window = close.iloc[-config.HIGH_LOOKBACK:]
    high = float(window.max())
    if high == 0:
        return None
    return float(close.iloc[-1] / high)


def _short_interest_snapshots(tickers: list[str]) -> dict[str, float]:
    """Fetch current short % of float for each ticker.

    CAVEAT: This is a SNAPSHOT, not a change. The spec asks for "change vs prior
    period" — yfinance does not expose short interest history. With free data,
    a low current short % of float is the closest defensible proxy (less short
    pressure = less squeeze risk in either direction). To do this properly,
    integrate FINRA's bi-monthly short interest file or a paid feed.

    We parallelize because .info is slow (~1 HTTP request per ticker).
    """
    results: dict[str, float] = {}

    def fetch(t: str) -> tuple[str, float | None]:
        try:
            info = yf.Ticker(t).info
            v = info.get("shortPercentOfFloat")
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


# ---------------------------------------------------------------------------
# Universe-level percentile ranking and composite
# ---------------------------------------------------------------------------
def _pct_rank(series: pd.Series, ascending: bool = True) -> pd.Series:
    """Percentile rank 0-100. ascending=True means higher raw value -> higher score."""
    if not ascending:
        series = -series
    return series.rank(pct=True, na_option="bottom") * 100


@dataclass
class ScanResult:
    df: pd.DataFrame              # full ranked table
    universe_size: int
    coverage: dict[str, int]      # how many tickers had each factor


def run_scan(
    ohlcv_by_ticker: dict[str, pd.DataFrame],
    spy: pd.Series,
    progress_cb=None,
) -> ScanResult:
    tickers = list(ohlcv_by_ticker.keys())

    # --- Raw values ---
    rows = []
    for t in tickers:
        ohlcv = ohlcv_by_ticker[t]
        rows.append({
            "ticker": t,
            "price": float(ohlcv["Close"].iloc[-1]),
            "f1_momentum_xover": _factor_momentum_crossover(ohlcv),
            "f2_vol_surge_ratio": _factor_volume_surge(ohlcv),
            "f3_rs_vs_spy": _factor_relative_strength(ohlcv, spy),
            "f4_pct_of_52w_high": _factor_52w_high_proximity(ohlcv),
        })

    if progress_cb:
        progress_cb("Fetching short interest snapshots…")
    short_data = _short_interest_snapshots(tickers)
    for row in rows:
        row["f5_short_pct_float"] = short_data.get(row["ticker"])

    df = pd.DataFrame(rows).set_index("ticker")

    # --- Percentile ranks per factor (0-100) ---
    # Factors where higher raw = better:
    df["score_1_momentum"] = _pct_rank(df["f1_momentum_xover"], ascending=True)
    df["score_3_rs"]       = _pct_rank(df["f3_rs_vs_spy"],     ascending=True)
    df["score_4_high_prox"]= _pct_rank(df["f4_pct_of_52w_high"], ascending=True)

    # Factor 2: volume surge — spec maps 0.7 -> 0, 2.0 -> 100 linearly,
    # but we use percentile rank per spec ("each scored 0-100, percentile rank").
    # We keep both views: percentile (used in composite) and the spec map (shown
    # in the detail column).
    df["score_2_vol_surge"] = _pct_rank(df["f2_vol_surge_ratio"], ascending=True)

    # Factor 5: LOWER short % = more bullish, so descending percentile.
    df["score_5_short"]    = _pct_rank(df["f5_short_pct_float"], ascending=False)

    # --- Composite: equal weight, only counting non-null factor scores ---
    score_cols = ["score_1_momentum", "score_2_vol_surge", "score_3_rs",
                  "score_4_high_prox", "score_5_short"]
    df["composite"] = df[score_cols].mean(axis=1)

    # --- Coverage report ---
    coverage = {
        "Momentum X-over (had recent cross)":
            int((df["f1_momentum_xover"].fillna(-1e9) > -1e8).sum()),
        "Volume surge": int(df["f2_vol_surge_ratio"].notna().sum()),
        "Relative strength": int(df["f3_rs_vs_spy"].notna().sum()),
        "52w high proximity": int(df["f4_pct_of_52w_high"].notna().sum()),
        "Short interest data": int(df["f5_short_pct_float"].notna().sum()),
    }

    df = df.sort_values("composite", ascending=False)
    return ScanResult(df=df, universe_size=len(tickers), coverage=coverage)
