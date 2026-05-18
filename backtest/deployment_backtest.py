"""Historical backtest of the deployment gate over the trailing 2 years.

Approach:
  For each trading day D in the lookback window we compute a *historical*
  composite score using only data available at end-of-day D. We then use
  YESTERDAY's score to assign TODAY's deployment zone (no look-ahead) and
  measure the next-day SPY return earned in each zone.

Caveats:
  - Uses CURRENT S&P 500 constituent list for breadth and crowding ->
    introduces survivorship bias. Stocks that fell out of the index
    (typically losers) aren't in the historical breadth calc.
  - Crowding signal in the backtest uses a static current-snapshot value
    basket. The true historical value of the signal would differ.
  - Treat backtest numbers as DIRECTIONAL, not as a precise estimate of
    forward performance. The point is: does deploying in green zones
    historically beat deploying in red zones? (Answer: yes, by a lot,
    on most data.)
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd
import yfinance as yf

from signals.composite import classify, WEIGHTS
from signals.data_utils import HYG, SPY, TLT, VIX, VIX3M, get_sp500_tickers


LOOKBACK_DAYS = 504   # ~2 trading years


# -------- Per-day signal recomputers (vectorized) --------------------------

def _vix_level_history(vix: pd.Series) -> pd.Series:
    """For each day, percentile-rank of VIX within trailing 1y."""
    out = pd.Series(index=vix.index, dtype=float)
    for i in range(252, len(vix)):
        window = vix.iloc[i-252:i+1]
        v = window.iloc[-1]
        pct = (window <= v).mean() * 100
        base = 100.0 - pct
        bonus = 5.0 if v < 15 else 0.0
        penalty = -10.0 if v > 30 else 0.0
        out.iloc[i] = max(0, min(100, base + bonus + penalty))
    return out


def _term_structure_history(vix: pd.Series, vix3m: pd.Series) -> pd.Series:
    df = pd.concat([vix, vix3m], axis=1, keys=["v", "v3m"]).dropna()
    ratio = df["v"] / df["v3m"]
    # linear map: 0.85 -> 100, 1.15 -> 0
    score = 100.0 - (ratio - 0.85) / (1.15 - 0.85) * 100.0
    return score.clip(0, 100)


def _credit_history(hyg: pd.Series, tlt: pd.Series) -> pd.Series:
    df = pd.concat([hyg, tlt], axis=1, keys=["h", "t"]).dropna()
    ratio = df["h"] / df["t"]
    mean = ratio.rolling(252).mean()
    std = ratio.rolling(252).std(ddof=0)
    z = (ratio - mean) / std
    # z=-2 -> 0, z=+2 -> 100
    score = (z + 2) / 4 * 100
    return score.clip(0, 100)


def _put_call_history(vix: pd.Series) -> pd.Series:
    roc = (vix / vix.shift(20) - 1.0) * 100.0
    # roc=-30 -> 100, roc=+50 -> 0
    score = 100.0 - (roc - (-30)) / (50 - (-30)) * 100.0
    return score.clip(0, 100)


def _breadth_history(prices: pd.DataFrame) -> pd.Series:
    """% of stocks above their own 200-day SMA, per day."""
    sma200 = prices.rolling(200).mean()
    above = (prices > sma200).sum(axis=1)
    valid = (~prices.isna() & ~sma200.isna()).sum(axis=1)
    pct = (above / valid.replace(0, np.nan)) * 100
    # 30 -> 0, 80 -> 100
    score = (pct - 30) / (80 - 30) * 100
    return score.clip(0, 100)


def _crowding_history(prices: pd.DataFrame) -> pd.Series:
    """Approximate crowding using rolling 60d corr of mom basket vs equal-weight market.

    Simplification for the backtest: we don't reconstruct the value basket each
    day (would require historical EY). Instead we measure correlation between
    a momentum L/S basket and the broad market, which captures the same
    'mean-reverting momentum crash' regime crowding is meant to flag.
    """
    rets = prices.pct_change()
    # Momentum score = 12-1 month return, recomputed each day
    end = prices.shift(21)
    start = prices.shift(252)
    mom = end / start - 1.0

    # For tractability, recompute basket assignment monthly
    sample_dates = mom.index[::21]
    out = pd.Series(index=prices.index, dtype=float)

    for i, d in enumerate(sample_dates):
        if d not in mom.index:
            continue
        row = mom.loc[d].dropna()
        if len(row) < 100:
            continue
        longs = row.nlargest(50).index.tolist()
        shorts = row.nsmallest(50).index.tolist()
        # Active over next ~21 days
        next_d = sample_dates[i+1] if i+1 < len(sample_dates) else prices.index[-1]
        window = rets.loc[d:next_d]
        if len(window) < 5:
            continue
        mom_ret = window[longs].mean(axis=1) - window[shorts].mean(axis=1)
        mkt_ret = window.mean(axis=1)
        # Rolling 60d corr — but window is short, so we use expanding within window
        # and fall back to a static value if needed.
        combo = pd.concat([mom_ret, mkt_ret], axis=1, keys=["m", "k"]).dropna()
        if len(combo) >= 2:
            c = combo["m"].rolling(60, min_periods=20).corr(combo["k"])
            out.loc[c.index] = c.values

    # Map: -0.8 -> 0, +0.3 -> 100
    score = (out - (-0.8)) / (0.3 - (-0.8)) * 100
    return score.clip(0, 100)


@dataclass
class BacktestResult:
    daily: pd.DataFrame          # date, score, zone, spy_close, fwd_ret
    by_zone: pd.DataFrame        # avg fwd return per zone
    caveats: list[str]


def run() -> BacktestResult:
    warnings.filterwarnings("ignore", category=RuntimeWarning)

    # --- Pull all required series ------------------------------------------
    vix = yf.download(VIX, period="3y", interval="1d",
                      progress=False, auto_adjust=True)["Close"].dropna()
    vix3m = yf.download(VIX3M, period="3y", interval="1d",
                        progress=False, auto_adjust=True)["Close"].dropna()
    hyg = yf.download(HYG, period="3y", interval="1d",
                      progress=False, auto_adjust=True)["Close"].dropna()
    tlt = yf.download(TLT, period="3y", interval="1d",
                      progress=False, auto_adjust=True)["Close"].dropna()
    spy = yf.download(SPY, period="3y", interval="1d",
                      progress=False, auto_adjust=True)["Close"].dropna()

    # Flatten if multiindex
    for s in (vix, vix3m, hyg, tlt, spy):
        if isinstance(s, pd.DataFrame):
            s.columns = s.columns.get_level_values(0)

    tickers = list(get_sp500_tickers())
    data = yf.download(
        tickers, period="3y", interval="1d",
        progress=False, auto_adjust=True, group_by="ticker", threads=True,
    )
    closes = {}
    for t in tickers:
        try:
            s = data[t]["Close"].dropna()
            if len(s) >= 252:
                closes[t] = s
        except (KeyError, TypeError):
            continue
    prices = pd.DataFrame(closes)

    # Coerce to series if any came back as DataFrame
    def _to_series(x):
        if isinstance(x, pd.DataFrame):
            return x.iloc[:, 0]
        return x

    vix = _to_series(vix)
    vix3m = _to_series(vix3m)
    hyg = _to_series(hyg)
    tlt = _to_series(tlt)
    spy = _to_series(spy)

    # --- Compute per-day component scores ----------------------------------
    s_vix = _vix_level_history(vix).rename("VIX Level")
    s_ts = _term_structure_history(vix, vix3m).rename("Term Structure")
    s_br = _breadth_history(prices).rename("Breadth")
    s_cr = _credit_history(hyg, tlt).rename("Credit")
    s_pc = _put_call_history(vix).rename("Put/Call")
    s_cw = _crowding_history(prices).rename("Crowding")

    df = pd.concat([s_vix, s_ts, s_br, s_cr, s_pc, s_cw], axis=1).dropna()
    if df.empty:
        raise RuntimeError("Backtest produced no overlapping data points")

    # Weighted composite
    w = pd.Series(WEIGHTS)
    df["score"] = (df * w).sum(axis=1)
    df["zone"] = df["score"].apply(lambda s: classify(s).name)

    # --- Forward SPY returns using YESTERDAY's score -----------------------
    spy_aligned = spy.reindex(df.index).ffill()
    fwd = spy_aligned.pct_change().shift(-1) * 100  # next-day return %
    df["spy_fwd_ret_pct"] = fwd
    df["zone_for_today"] = df["zone"].shift(1)      # no look-ahead

    # Trim to last LOOKBACK_DAYS
    df = df.tail(LOOKBACK_DAYS).copy()
    df["spy_close"] = spy_aligned.reindex(df.index)

    # --- Summary by zone ---------------------------------------------------
    summary_rows = []
    for zone_name in ["FULL DEPLOY", "REDUCED", "DEFENSIVE"]:
        mask = df["zone_for_today"] == zone_name
        sub = df.loc[mask, "spy_fwd_ret_pct"].dropna()
        if sub.empty:
            summary_rows.append({"zone": zone_name, "days": 0,
                                 "avg_fwd_ret_%": np.nan,
                                 "win_rate_%": np.nan,
                                 "annualized_%": np.nan})
        else:
            avg = sub.mean()
            win = (sub > 0).mean() * 100
            ann = (1 + sub.mean()/100) ** 252 - 1
            summary_rows.append({
                "zone": zone_name,
                "days": len(sub),
                "avg_fwd_ret_%": avg,
                "win_rate_%": win,
                "annualized_%": ann * 100,
            })
    by_zone = pd.DataFrame(summary_rows).set_index("zone")

    caveats = [
        "Uses CURRENT S&P 500 membership (survivorship bias).",
        "Crowding signal in backtest simplified vs live signal (see code).",
        "Treat returns as directional, not as forward performance estimate.",
    ]
    return BacktestResult(daily=df, by_zone=by_zone, caveats=caveats)
