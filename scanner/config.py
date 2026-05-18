"""Scanner configuration — values match the L2 spec.

5 factors, equal-weighted, percentile-ranked across the S&P 500 universe.
Gated by the macro deployment score:
  FULL DEPLOY  -> return full ranked list
  REDUCED      -> only stocks with composite >= 75
  DEFENSIVE    -> scanner disabled, return empty list
"""
from __future__ import annotations

# Universe
UNIVERSE = "SP500"
PRICE_LOOKBACK = "1y"

# Factor 1 — Momentum Crossover (10 EMA / 50 EMA)
EMA_FAST = 10
EMA_SLOW = 50
CROSSOVER_WINDOW = 5      # crossover must have happened in last N days

# Factor 2 — Volume Surge
VOL_SHORT = 5             # 5-day avg volume
VOL_LONG = 20             # 20-day avg volume
# Mapping: 0.7 -> 0, 2.0 -> 100 (linear, clamped)

# Factor 3 — Relative Strength vs SPY
RS_LOOKBACK = 20          # 20-day return spread

# Factor 4 — 52-Week High Proximity
HIGH_LOOKBACK = 252       # ~52 weeks of trading days

# Factor 5 — Short Interest Decline
# NOTE: implemented as a level proxy from yfinance snapshot (current
# shortPercentOfFloat). Lower = more bullish. See scoring.py for caveats.

# Gating thresholds
REDUCED_MIN_COMPOSITE = 75.0

# Output
TOP_N_DISPLAY = 25        # show top N in dashboard and email
