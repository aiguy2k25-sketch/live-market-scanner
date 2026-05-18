# Macro Deployment Gate

A 6-signal macro gating system that answers: **"Should I be deploying capital right now, and how aggressively?"**

Pulls 6 macro signals, scores each 0–100, and blends into a single deployment
score that maps to a sizing zone.

## Signals & weights

| # | Signal           | Weight | What it measures                                     |
|---|------------------|--------|------------------------------------------------------|
| 1 | VIX Level        | 0.25   | Trailing-1y percentile of VIX (low VIX = high score) |
| 2 | VIX Term Structure | 0.20 | VIX / VIX3M (contango = calm, backwardation = stress)|
| 3 | Market Breadth   | 0.20   | % of S&P 500 above 200-day SMA                       |
| 4 | Credit Spreads   | 0.15   | HYG/TLT ratio z-score (tight = good)                 |
| 5 | Put/Call         | 0.10   | VIX 20-day rate of change (fear acceleration proxy)  |
| 6 | Factor Crowding  | 0.10   | 60d corr of momentum L/S vs value L/S baskets        |

## Deployment zones

| Score    | Zone         | Action                                                |
|----------|--------------|-------------------------------------------------------|
| 70–100   | FULL DEPLOY  | 100% sizing                                           |
| 40–69    | REDUCED      | 60% sizing, higher bar for new positions              |
| 0–39     | DEFENSIVE    | 25% sizing, no new longs, scanner disabled            |

## Install

```bash
cd macro_gate
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

## Run

```bash
streamlit run run_macro_gate.py
```

The dashboard opens at http://localhost:8501. First load takes ~30s because the
breadth and crowding signals download ~500 S&P 500 tickers. Subsequent loads
are cached (15 min for live signals, 1 hour for the backtest).

## Project layout

```
macro_gate/
├── run_macro_gate.py            # Streamlit dashboard entry point
├── requirements.txt
├── signals/
│   ├── __init__.py
│   ├── data_utils.py            # Shared yfinance helpers
│   ├── vix_level.py             # Signal 1
│   ├── vix_term_structure.py    # Signal 2
│   ├── breadth.py               # Signal 3
│   ├── credit_spreads.py        # Signal 4
│   ├── put_call.py              # Signal 5
│   ├── crowding.py              # Signal 6
│   └── composite.py             # Weighted blend + zone classification
└── backtest/
    ├── __init__.py
    └── deployment_backtest.py   # 2y historical backtest, no look-ahead
```

## Caveats — read these

1. **Survivorship bias.** Breadth and crowding use the CURRENT S&P 500
   constituent list. Stocks that fell out of the index (usually losers) are
   missing from historical calcs, which biases the backtest favorably.
2. **Value basket is a snapshot.** The Factor Crowding signal uses current
   trailing P/E from Yahoo as a value proxy. A real value factor uses
   point-in-time book/price. The live signal is fine; the backtest's crowding
   numbers are approximate.
3. **Backtest is directional.** It shows whether deploying in green zones
   historically beat red zones (it does, substantially, on most data). It's
   NOT a precise forward-return estimate.
4. **No transaction costs / slippage** in the backtest.
5. **Yahoo Finance is free but flaky.** If a signal fails, the composite
   renormalizes weights over the remaining signals. Persistent failures
   warrant switching to a paid feed.

## Swapping data sources

All ticker access lives in `signals/data_utils.py`. To swap to a paid feed
(Polygon, EOD Historical Data, Tiingo, etc.), replace `_download`,
`get_close`, and `get_sp500_tickers` and everything else continues to work.
