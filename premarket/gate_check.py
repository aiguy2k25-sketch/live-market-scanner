"""
gate_check.py  —  pre-trade gate stamper for live-market-scanner

Drop this in:  premarket/gate_check.py
Then in premarket_bot.py:
    from gate_check import run_gate_batch, format_email_section
    reports = run_gate_batch(candidate_tickers, regime=RegimeContext(red_day=is_red_day))
    email_body += format_email_section(reports)

Standalone test (no wiring needed):
    python gate_check.py NVDA AMD HPE SHAZ

Design rules:
  * FAILS CLOSED. Missing/broken data -> DATA_ERROR, never a silent PASS.
  * Per-ticker technical gates run automatically off yfinance.
  * Portfolio gates (factor cap, cooldown) need state you pass in; without it
    they WARN, they do not fake a PASS.
  * All thresholds live in CONFIG so you can tune from the GitHub web UI.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

# ----------------------------------------------------------------------------
# CONFIG  — tune these; they map 1:1 to the pre-trade checklist
# ----------------------------------------------------------------------------
CONFIG = {
    "MIN_PRICE": 5.0,               # gate 1
    "MIN_MARKET_CAP": 500_000_000,  # gate 1
    "MIN_RVOL": 1.5,                # gate 1
    "MIN_AVG_DOLLAR_VOL": 20_000_000,  # gate 1  (20-day avg $ volume)
    "MIN_DAYS_TRADABLE": 65,        # gate 2  (~13 weeks)
    "MIN_DAYS_STAGEABLE": 150,      # gate 2  (~30 weeks, for Weinstein staging)
    "MAX_ATR_EXTENSION": 2.0,       # gate 3  (ATRs above the 20-day SMA)
    "MAX_STOP_PCT": 0.10,           # gate 7  (hard stop no wider than 10%)
    "STRUCTURE_MAX_PCT": 0.12,      # gate 3/7 (nearest 10-day low within 12%)
    "SEMI_CAP_PCT": 30.0,           # gate 5  (semi factor share of invested)
    "HISTORY_PERIOD": "1y",         # yfinance lookback for bars
}

# Gate 5 — everything here is ONE bet. A Korea/DRAM headline moves them together.
SEMI_FACTOR: Set[str] = {
    "AMD", "NVDA", "SKHY", "SKHYV", "DRAM", "AMKR", "KLIC", "CRDO", "SMCI",
    "INTC", "WOLF", "NVTS", "AEHR", "POET", "MU", "TSM", "AVGO", "ASML",
    "LRCX", "AMAT", "QCOM", "ARM", "MRVL", "ON", "TXN", "MCHP",
    "SMH", "SOXX", "SOXL", "XSD",  # semi ETFs
}

# Gate 4 — hard veto. Past-you already ruled these out.
DO_NOT_TRADE: Set[str] = {"SHAZ", "FRVO"}

# Gate 4 — soft flag. Names you've flagged before but may still trade.
# Produces a WARN ("re-verify thesis"), not a ban.
WATCH_FLAGGED: Set[str] = {"SKHY", "SKHYV", "INTC"}


# ----------------------------------------------------------------------------
# Data containers
# ----------------------------------------------------------------------------
@dataclass
class MarketData:
    ticker: str
    price: Optional[float] = None
    market_cap: Optional[float] = None
    rvol: Optional[float] = None
    avg_dollar_volume: Optional[float] = None
    trading_days: Optional[int] = None
    sma10: Optional[float] = None
    sma20: Optional[float] = None
    atr14: Optional[float] = None
    low_10d: Optional[float] = None
    ok: bool = True
    error: str = ""


@dataclass
class PortfolioContext:
    """Optional. Pass what you have; missing pieces WARN instead of failing."""
    semi_exposure_pct: Optional[float] = None   # current semi % of invested
    open_positions: Set[str] = field(default_factory=set)
    cooldown: Set[str] = field(default_factory=set)  # net losers / >2 round-trips this month


@dataclass
class RegimeContext:
    red_day: bool = False
    note: str = ""


@dataclass
class GateOutcome:
    name: str
    status: str    # PASS / FAIL / WARN / SKIP
    detail: str


@dataclass
class GateReport:
    ticker: str
    verdict: str            # PASS / FAIL / WARN / DATA_ERROR
    gates: List[GateOutcome]
    flags: List[str]
    suggested_stop: Optional[float] = None
    stop_pct: Optional[float] = None


# ----------------------------------------------------------------------------
# Individual gates  (pure functions of MarketData / context — easy to test)
# ----------------------------------------------------------------------------
def _g1_liquidity(md: MarketData) -> GateOutcome:
    fails = []
    if md.price is None or md.price <= CONFIG["MIN_PRICE"]:
        fails.append(f"price {md.price}")
    if md.market_cap is None or md.market_cap < CONFIG["MIN_MARKET_CAP"]:
        cap = f"{md.market_cap/1e6:.0f}M" if md.market_cap else "n/a"
        fails.append(f"mcap {cap}")
    if md.rvol is None or md.rvol < CONFIG["MIN_RVOL"]:
        fails.append(f"rvol {md.rvol}")
    if md.avg_dollar_volume is None or md.avg_dollar_volume < CONFIG["MIN_AVG_DOLLAR_VOL"]:
        adv = f"${md.avg_dollar_volume/1e6:.0f}M" if md.avg_dollar_volume else "n/a"
        fails.append(f"advol {adv}")
    if fails:
        return GateOutcome("liquidity", "FAIL", ", ".join(fails))
    return GateOutcome("liquidity", "PASS",
                       f"${md.price:.2f}, {md.market_cap/1e6:.0f}M, rvol {md.rvol:.1f}")


def _g2_history(md: MarketData) -> GateOutcome:
    d = md.trading_days
    if d is None or d < CONFIG["MIN_DAYS_TRADABLE"]:
        return GateOutcome("history", "FAIL", f"{d} trading days (<{CONFIG['MIN_DAYS_TRADABLE']})")
    if d < CONFIG["MIN_DAYS_STAGEABLE"]:
        return GateOutcome("history", "WARN", f"{d} days — tradable but not stageable (<30wk)")
    return GateOutcome("history", "PASS", f"{d} days")


def _g3_extension(md: MarketData) -> GateOutcome:
    if md.price is None or md.sma20 is None or md.atr14 is None or md.atr14 <= 0:
        return GateOutcome("extended", "FAIL", "insufficient data")
    atrs_above = (md.price - md.sma20) / md.atr14
    if atrs_above > CONFIG["MAX_ATR_EXTENSION"]:
        return GateOutcome("extended", "FAIL", f"{atrs_above:.1f} ATR above 20-day (chasing)")
    if md.low_10d is not None and md.price > 0:
        structure_pct = (md.price - md.low_10d) / md.price
        if structure_pct > CONFIG["STRUCTURE_MAX_PCT"]:
            return GateOutcome("extended", "WARN",
                               f"nearest 10-day low is {structure_pct*100:.0f}% away")
    return GateOutcome("extended", "PASS", f"{atrs_above:.1f} ATR above 20-day")


def _g4_flagged(md: MarketData) -> GateOutcome:
    t = md.ticker.upper()
    if t in DO_NOT_TRADE:
        return GateOutcome("no-list", "FAIL", "on DO_NOT_TRADE")
    if t in WATCH_FLAGGED:
        return GateOutcome("no-list", "WARN", "previously flagged — re-verify thesis")
    return GateOutcome("no-list", "PASS", "clear")


def _g5_factor(md: MarketData, pf: Optional[PortfolioContext]) -> GateOutcome:
    is_semi = md.ticker.upper() in SEMI_FACTOR
    if not is_semi:
        return GateOutcome("factor-cap", "PASS", "not a semi-factor name")
    if pf is None or pf.semi_exposure_pct is None:
        return GateOutcome("factor-cap", "WARN",
                           "SEMI-FACTOR — supply semi_exposure_pct to enforce 30% cap")
    if pf.semi_exposure_pct >= CONFIG["SEMI_CAP_PCT"]:
        return GateOutcome("factor-cap", "FAIL",
                           f"semi exposure {pf.semi_exposure_pct:.0f}% >= {CONFIG['SEMI_CAP_PCT']:.0f}% — trim first")
    return GateOutcome("factor-cap", "PASS",
                       f"SEMI-FACTOR, exposure {pf.semi_exposure_pct:.0f}% (< cap)")


def _g6_cooldown(md: MarketData, pf: Optional[PortfolioContext]) -> GateOutcome:
    t = md.ticker.upper()
    if pf is None:
        return GateOutcome("cooldown", "WARN", "no portfolio state — check open/round-trips manually")
    if t in pf.open_positions:
        return GateOutcome("cooldown", "FAIL", "already an open position")
    if t in pf.cooldown:
        return GateOutcome("cooldown", "FAIL", "on cooldown (net loser / >2 round-trips this month)")
    return GateOutcome("cooldown", "PASS", "clear")


def _g7_stop(md: MarketData) -> GateOutcome:
    if md.price is None:
        return GateOutcome("stop", "FAIL", "no price")
    hard_stop = md.price * (1 - CONFIG["MAX_STOP_PCT"])
    stop = hard_stop
    detail = f"10% stop ${stop:.2f}"
    if md.low_10d is not None and md.low_10d >= hard_stop and md.low_10d < md.price:
        stop = md.low_10d
        detail = f"structure stop ${stop:.2f} (10-day low)"
    return GateOutcome("stop", "PASS", detail)


# ----------------------------------------------------------------------------
# Evaluator
# ----------------------------------------------------------------------------
_HARD_GATES = {"liquidity", "history", "extended", "no-list", "factor-cap", "cooldown", "stop"}


def evaluate_ticker(md: MarketData,
                    pf: Optional[PortfolioContext] = None,
                    regime: Optional[RegimeContext] = None) -> GateReport:
    if not md.ok:
        return GateReport(md.ticker, "DATA_ERROR",
                          [GateOutcome("data", "FAIL", md.error or "fetch failed")], [])

    gates = [
        _g1_liquidity(md),
        _g2_history(md),
        _g3_extension(md),
        _g4_flagged(md),
        _g5_factor(md, pf),
        _g6_cooldown(md, pf),
        _g7_stop(md),
    ]

    flags = []
    if md.ticker.upper() in SEMI_FACTOR:
        flags.append("SEMI-FACTOR")

    stop_gate = gates[-1]
    suggested_stop = None
    stop_pct = None
    if md.price:
        # parse the stop we computed back out for the report
        suggested_stop = md.price * (1 - CONFIG["MAX_STOP_PCT"])
        if md.low_10d is not None and md.low_10d >= suggested_stop and md.low_10d < md.price:
            suggested_stop = md.low_10d
        stop_pct = (md.price - suggested_stop) / md.price

    has_hard_fail = any(g.status == "FAIL" and g.name in _HARD_GATES for g in gates)
    has_warn = any(g.status == "WARN" for g in gates)

    if has_hard_fail:
        verdict = "FAIL"
    elif regime is not None and regime.red_day:
        verdict = "WARN"
        gates.append(GateOutcome("regime", "WARN", "red-market day — no new entries"))
    elif has_warn:
        verdict = "WARN"
    else:
        verdict = "PASS"

    return GateReport(md.ticker, verdict, gates, flags, suggested_stop, stop_pct)


# ----------------------------------------------------------------------------
# Data fetch (yfinance)  — deferred import so the module loads without it
# ----------------------------------------------------------------------------
def fetch_market_data(ticker: str, rvol_override: Optional[float] = None) -> MarketData:
    """Pull everything the gates need. Fails closed: any exception -> ok=False."""
    try:
        import yfinance as yf
    except ImportError:
        return MarketData(ticker, ok=False, error="yfinance not installed")

    try:
        t = yf.Ticker(ticker)
        hist = t.history(period=CONFIG["HISTORY_PERIOD"], auto_adjust=False)
        if hist is None or len(hist) < 20:
            return MarketData(ticker, ok=False, error="insufficient price history")

        closes = hist["Close"].tolist()
        highs = hist["High"].tolist()
        lows = hist["Low"].tolist()
        vols = hist["Volume"].tolist()

        price = closes[-1]
        trading_days = len(closes)
        sma10 = sum(closes[-10:]) / 10
        sma20 = sum(closes[-20:]) / 20
        low_10d = min(lows[-10:])

        # ATR(14) via true range
        trs = []
        for i in range(len(closes) - 14, len(closes)):
            if i <= 0:
                continue
            tr = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))
            trs.append(tr)
        atr14 = sum(trs) / len(trs) if trs else None

        # RVOL: latest volume vs prior 20-day average (override if you have better data)
        if rvol_override is not None:
            rvol = rvol_override
        else:
            prior_avg = sum(vols[-21:-1]) / 20 if len(vols) >= 21 else None
            rvol = (vols[-1] / prior_avg) if prior_avg else None

        avg_dollar_volume = sum(closes[-20:][i] * vols[-20:][i] for i in range(20)) / 20

        # market cap via fast_info, fallback to info
        market_cap = None
        try:
            market_cap = t.fast_info.get("market_cap")
        except Exception:
            pass
        if not market_cap:
            try:
                market_cap = t.info.get("marketCap")
            except Exception:
                pass

        return MarketData(
            ticker=ticker, price=price, market_cap=market_cap, rvol=rvol,
            avg_dollar_volume=avg_dollar_volume, trading_days=trading_days,
            sma10=sma10, sma20=sma20, atr14=atr14, low_10d=low_10d, ok=True,
        )
    except Exception as e:  # fail closed
        return MarketData(ticker, ok=False, error=f"{type(e).__name__}: {e}")


# ----------------------------------------------------------------------------
# Batch + formatting for the email
# ----------------------------------------------------------------------------
def run_gate_batch(tickers: List[str],
                   pf: Optional[PortfolioContext] = None,
                   regime: Optional[RegimeContext] = None,
                   rvol_overrides: Optional[Dict[str, float]] = None) -> List[GateReport]:
    rvol_overrides = rvol_overrides or {}
    reports = []
    for tk in tickers:
        md = fetch_market_data(tk, rvol_override=rvol_overrides.get(tk.upper()))
        reports.append(evaluate_ticker(md, pf=pf, regime=regime))
    order = {"PASS": 0, "WARN": 1, "FAIL": 2, "DATA_ERROR": 3}
    reports.sort(key=lambda r: order.get(r.verdict, 9))
    return reports


def _mark(status: str) -> str:
    return {"PASS": "+", "FAIL": "x", "WARN": "!", "SKIP": "-"}.get(status, "?")


def stamp_line(r: GateReport) -> str:
    gate_str = " ".join(f"{_mark(g.status)}{g.name}" for g in r.gates)
    flag_str = (" | " + ",".join(r.flags)) if r.flags else ""
    stop_str = ""
    if r.suggested_stop is not None:
        stop_str = f" | stop ${r.suggested_stop:.2f} (-{r.stop_pct*100:.0f}%)"
    return f"[{r.verdict:10}] {r.ticker:6} {gate_str}{flag_str}{stop_str}"


def stamp_block(r: GateReport) -> str:
    lines = [stamp_line(r)]
    for g in r.gates:
        if g.status in ("FAIL", "WARN"):
            lines.append(f"    {_mark(g.status)} {g.name}: {g.detail}")
    return "\n".join(lines)


def format_email_section(reports: List[GateReport], detail: bool = True) -> str:
    passed = sum(1 for r in reports if r.verdict == "PASS")
    warned = sum(1 for r in reports if r.verdict == "WARN")
    failed = sum(1 for r in reports if r.verdict in ("FAIL", "DATA_ERROR"))
    head = ("\n" + "=" * 60 +
            f"\nPRE-TRADE GATES  —  {passed} pass / {warned} warn / {failed} fail\n" +
            "=" * 60 + "\n")
    body = "\n".join((stamp_block(r) if detail else stamp_line(r)) for r in reports)
    legend = ("\n\nlegend: + pass  ! warn (check)  x fail  |  gates: "
              "liquidity history extended no-list factor-cap cooldown stop")
    return head + body + legend


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    tickers = [a.upper() for a in sys.argv[1:]] or ["NVDA", "AMD", "HPE", "SHAZ"]
    reports = run_gate_batch(tickers)
    print(format_email_section(reports))
