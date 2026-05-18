"""Top-level scanner runner.

Reads the current macro deployment score, applies gating rules, and returns
either a full ranked table, a filtered table, or an empty disabled result.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from signals.composite import compute as compute_composite

from . import config
from .scoring import ScanResult, run_scan
from .universe import download_ohlcv, get_spy, get_universe


@dataclass
class GatedScanResult:
    zone: str                      # FULL DEPLOY | REDUCED | DEFENSIVE
    score: float                   # composite macro score
    disabled: bool                 # True if DEFENSIVE
    df: pd.DataFrame               # ranked table (possibly filtered or empty)
    universe_size: int
    coverage: dict[str, int]
    notes: list[str]


def run(progress_cb=None) -> GatedScanResult:
    """Run the full scanner pipeline with macro gate integration."""
    notes: list[str] = []

    # --- Step 1: read the gate ---
    if progress_cb:
        progress_cb("Reading macro deployment score…")
    comp = compute_composite()
    zone = comp.zone.name
    notes.append(f"Macro composite score: {comp.score:.1f} ({zone})")

    if zone == "DEFENSIVE":
        notes.append("Scanner DISABLED — defensive zone, no new longs.")
        return GatedScanResult(
            zone=zone, score=comp.score, disabled=True,
            df=pd.DataFrame(), universe_size=0, coverage={}, notes=notes,
        )

    # --- Step 2: download data ---
    if progress_cb:
        progress_cb("Loading universe…")
    tickers = get_universe()

    if progress_cb:
        progress_cb(f"Downloading OHLCV for {len(tickers)} tickers…")
    ohlcv = download_ohlcv(tickers)

    if progress_cb:
        progress_cb("Fetching SPY benchmark…")
    spy = get_spy()

    # --- Step 3: score ---
    if progress_cb:
        progress_cb("Computing 5-factor scores…")
    result = run_scan(ohlcv, spy, progress_cb=progress_cb)
    df = result.df

    # --- Step 4: gate filtering ---
    if zone == "REDUCED":
        before = len(df)
        df = df[df["composite"] >= config.REDUCED_MIN_COMPOSITE].copy()
        notes.append(
            f"REDUCED zone: surfacing only composite >= "
            f"{config.REDUCED_MIN_COMPOSITE:.0f} ({len(df)}/{before} qualify)."
        )
    else:  # FULL DEPLOY
        notes.append("FULL DEPLOY: full ranked list returned.")

    return GatedScanResult(
        zone=zone,
        score=comp.score,
        disabled=False,
        df=df,
        universe_size=result.universe_size,
        coverage=result.coverage,
        notes=notes,
    )
