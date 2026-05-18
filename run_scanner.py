"""CLI entry point: run scanner, print results, save CSV.

Used by GitHub Actions for scheduled scans and email delivery.
Can also be run locally:
    python run_scanner.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path

import pandas as pd

from scanner import run as run_scanner
from scanner.config import TOP_N_DISPLAY


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the quant scanner")
    parser.add_argument("--out", default="results",
                        help="Output directory for CSVs (default: results/)")
    parser.add_argument("--top", type=int, default=TOP_N_DISPLAY,
                        help=f"Top N to print (default: {TOP_N_DISPLAY})")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*70}")
    print(f" QUANTITATIVE SCANNER  -  {dt.datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*70}\n")

    result = run_scanner(progress_cb=lambda msg: print(f"  - {msg}"))

    print()
    for note in result.notes:
        print(f"  {note}")
    print()

    if result.disabled:
        print("  Scanner disabled. Nothing to surface.\n")
        marker = out_dir / f"scan_{dt.date.today():%Y%m%d}_DISABLED.txt"
        marker.write_text(
            f"Scanner disabled - macro composite {result.score:.1f} "
            f"({result.zone}).\n"
        )
        return 0

    df = result.df.head(args.top)

    display_cols = [
        "price", "composite",
        "score_1_momentum", "score_2_vol_surge",
        "score_3_rs", "score_4_high_prox", "score_5_inst",
    ]
    pretty = df[display_cols].copy()
    pretty.columns = ["Price", "Composite",
                      "Momentum", "Vol Surge", "RS", "52w Hi", "Inst%"]
    pretty = pretty.round(1)
    print(f"  Top {len(pretty)} of {result.universe_size} universe:\n")
    print(pretty.to_string())
    print()

    print("  Factor coverage (tickers with usable data):")
    for k, v in result.coverage.items():
        pct = 100 * v / result.universe_size if result.universe_size else 0
        print(f"    - {k:<40} {v:>4} ({pct:.0f}%)")
    print()

    today = dt.date.today()
    full_path = out_dir / f"scan_{today:%Y%m%d}_full.csv"
    top_path = out_dir / f"scan_{today:%Y%m%d}_top{args.top}.csv"
    result.df.to_csv(full_path)
    df.to_csv(top_path)
    print(f"  Saved: {full_path}")
    print(f"  Saved: {top_path}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
