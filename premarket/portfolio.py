"""
portfolio.py  —  fills the discipline-section hook premarket_bot.py already calls.

premarket_bot.py does (unchanged):
    import portfolio as _portfolio
    ...
    portfolio_plain, portfolio_html, portfolio_flags = \
        _portfolio.build_discipline_section(watchlist_tickers=all_tickers)

This module implements exactly that signature by running gate_check over the
morning's candidates. Drop BOTH files into premarket/:
    premarket/gate_check.py
    premarket/portfolio.py
No edits to premarket_bot.py are needed — the hook is already there.

Optional control file (edit from the GitHub web UI, one ticker per line):
    premarket/cooldown.txt   -> names on cooldown (net losers / >2 round-trips this month)

Returns:
    plain (str)   appended to the plain-text email
    html  (str)   injected into the HTML email above the ranked table
    flags (dict)  {ticker: [badges]} -> per-row badges. "COOLDOWN" renders orange
                  in the bot; "SEMI" / "NO-TRADE" / "GATE-FAIL" render gray/red.
"""

from __future__ import annotations

import os
from typing import Dict, List, Tuple

# Only gate the actionable top of the list — keeps yfinance calls bounded so the
# 8:03 delivery doesn't slip. Raise if you want the whole list stamped.
MAX_GATE_TICKERS = 15

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_cooldown() -> set:
    path = os.path.join(_HERE, "cooldown.txt")
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {ln.strip().upper() for ln in f
                    if ln.strip() and not ln.strip().startswith("#")}
    except Exception:
        return set()


def build_discipline_section(watchlist_tickers: List[str]
                             ) -> Tuple[str, str, Dict[str, list]]:
    # Fail safe for the EMAIL rendering: if gate_check is missing, degrade to
    # empty section rather than crashing the scan. (Gate VERDICTS still fail
    # closed inside gate_check itself — this only guards the email block.)
    try:
        from gate_check import (run_gate_batch, PortfolioContext,
                                 SEMI_FACTOR, stamp_line)
    except Exception as e:
        print(f"[PORTFOLIO] gate_check unavailable: {e}")
        return "", "", {}

    tickers = [t.upper() for t in watchlist_tickers][:MAX_GATE_TICKERS]
    if not tickers:
        return "", "", {}

    pf = PortfolioContext(cooldown=_load_cooldown())
    reports = run_gate_batch(tickers, pf=pf)

    n_pass = sum(1 for r in reports if r.verdict == "PASS")
    n_warn = sum(1 for r in reports if r.verdict == "WARN")
    n_fail = sum(1 for r in reports if r.verdict in ("FAIL", "DATA_ERROR"))
    semis = [r.ticker for r in reports if "SEMI-FACTOR" in r.flags]

    # ---- per-ticker badges for the ranked table --------------------------
    flags: Dict[str, list] = {}
    for r in reports:
        badges = []
        if "SEMI-FACTOR" in r.flags:
            badges.append("SEMI")
        if r.verdict in ("FAIL", "DATA_ERROR"):
            cd = any(g.name == "cooldown" and g.status == "FAIL" for g in r.gates)
            nl = any(g.name == "no-list" and g.status == "FAIL" for g in r.gates)
            if cd:
                badges.append("COOLDOWN")
            elif nl:
                badges.append("NO-TRADE")
            else:
                badges.append("GATE-FAIL")
        if badges:
            flags[r.ticker] = badges

    # ---- plain text ------------------------------------------------------
    pl = []
    pl.append("PRE-TRADE GATES  (top %d candidates)" % len(tickers))
    pl.append("-" * 72)
    pl.append("%d pass  /  %d warn  /  %d fail" % (n_pass, n_warn, n_fail))
    if semis:
        pl.append("semi-factor: %d of %d are the SAME bet -> %s"
                  % (len(semis), len(tickers), ", ".join(semis)))
    pl.append("")
    for r in reports:
        pl.append(stamp_line(r))
        for g in r.gates:
            if g.status in ("FAIL", "WARN"):
                pl.append("      %s: %s" % (g.name, g.detail))
    plain = "\n".join(pl)

    # ---- html ------------------------------------------------------------
    verdict_color = {"PASS": "#28a745", "WARN": "#fd7e14",
                     "FAIL": "#dc3545", "DATA_ERROR": "#6c757d"}
    rows = []
    for r in reports:
        vc = verdict_color.get(r.verdict, "#6c757d")
        reasons = "; ".join("%s: %s" % (g.name, g.detail)
                            for g in r.gates if g.status in ("FAIL", "WARN")) or "&mdash;"
        stop = ("$%.2f (-%d%%)" % (r.suggested_stop, round(r.stop_pct * 100))
                if r.suggested_stop else "&mdash;")
        rows.append(
            '<tr>'
            '<td style="padding:4px 8px;font-weight:bold">%s</td>'
            '<td style="padding:4px 8px;color:%s;font-weight:bold">%s</td>'
            '<td style="padding:4px 8px;font-size:12px;color:#555">%s</td>'
            '<td style="padding:4px 8px;font-size:12px;white-space:nowrap">%s</td>'
            '</tr>' % (r.ticker, vc, r.verdict, reasons, stop)
        )

    semi_line = ""
    if semis:
        semi_line = (
            '<div style="margin:6px 0 10px;font-size:13px;color:#721c24">'
            '<strong>Semi-factor concentration:</strong> %d of %d candidates are the '
            'same bet (%s) &mdash; a single Korea/DRAM headline moves them together.'
            '</div>' % (len(semis), len(tickers), ", ".join(semis))
        )

    html = (
        '<div style="background:#fff3cd;border-left:4px solid #fd7e14;'
        'padding:12px 16px;border-radius:4px;margin-bottom:20px">'
        '<strong style="font-size:14px">PRE-TRADE GATES</strong> '
        '<span style="font-size:13px;color:#555">&mdash; %d pass / %d warn / %d fail '
        '(top %d)</span>'
        '%s'
        '<table style="width:100%%;border-collapse:collapse;font-size:13px;margin-top:6px">'
        '<thead><tr style="text-align:left;color:#666;font-size:11px">'
        '<th style="padding:4px 8px">Ticker</th><th style="padding:4px 8px">Gate</th>'
        '<th style="padding:4px 8px">Why</th><th style="padding:4px 8px">Stop</th>'
        '</tr></thead><tbody>%s</tbody></table>'
        '<div style="font-size:11px;color:#856404;margin-top:6px">'
        'Gates: liquidity, history, extended, no-list, factor-cap, cooldown, stop. '
        'Add names to premarket/cooldown.txt to enforce the cooldown gate.</div>'
        '</div>'
        % (n_pass, n_warn, n_fail, len(tickers), semi_line, "".join(rows))
    )

    return plain, html, flags


if __name__ == "__main__":
    import sys
    tks = [a.upper() for a in sys.argv[1:]] or ["NVDA", "AMD", "HPE", "SHAZ"]
    p, h, f = build_discipline_section(tks)
    print(p)
    print("\nFLAGS:", f)
