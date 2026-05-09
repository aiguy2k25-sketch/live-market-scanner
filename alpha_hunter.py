"""
Pristine Alpha Hunter — institutional sweep scanner (yfinance edition).

Scans a universe of liquid tickers for options contracts with unusually high
volume vs open interest — a free proxy for institutional sweep activity.
Checks each hit against Perplexity for real-time news, then has Claude
reason about whether it looks like informed money with no public catalyst.

Environment variables:
    ANTHROPIC_API_KEY   — your Anthropic key
    PERPLEXITY_API_KEY  — your Perplexity key
"""
import argparse
import asyncio
import json
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
import yfinance as yf
from anthropic import AsyncAnthropic
from pydantic import BaseModel

PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"
MODEL = "claude-opus-4-7"
MAX_TOKENS = 2048

# Universe of liquid tickers with active options markets
DEFAULT_TICKERS = [
    # Broad ETFs
    "SPY", "QQQ", "IWM", "GLD", "TLT", "XLF", "XLE", "XLK", "XLV", "XBI",
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL", "AVGO", "ORCL",
    # Semiconductors
    "AMD", "INTC", "MU", "QCOM", "ARM", "SMCI",
    # Financials
    "JPM", "GS", "BAC", "MS", "C", "WFC",
    # Energy
    "XOM", "CVX", "OXY",
    # Biotech / Pharma
    "LLY", "MRNA", "PFE", "ABBV", "REGN", "BIIB",
    # Consumer / Retail
    "NFLX", "COST", "WMT", "TGT",
    # High-vol / momentum
    "COIN", "HOOD", "PLTR", "UBER", "SNOW", "RBLX",
    # Chinese ADRs
    "BABA", "JD", "PDD",
]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class SweepAnalysis(BaseModel):
    is_pristine_alpha: bool
    confidence: float
    catalyst_found: bool
    catalyst_description: str
    reasoning: str


ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "is_pristine_alpha": {
            "type": "boolean",
            "description": "True when premium is large and no credible public catalyst exists"
        },
        "confidence": {
            "type": "number",
            "description": "Confidence score from 0.0 to 1.0"
        },
        "catalyst_found": {
            "type": "boolean",
            "description": "True if a plausible public catalyst (earnings, FDA, M&A rumor) was found"
        },
        "catalyst_description": {
            "type": "string",
            "description": "Brief description of the catalyst, or empty string if none"
        },
        "reasoning": {
            "type": "string",
            "description": "Two or three sentences explaining the rating"
        }
    },
    "required": [
        "is_pristine_alpha", "confidence", "catalyst_found",
        "catalyst_description", "reasoning"
    ]
}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sweeps (
            id                   TEXT PRIMARY KEY,
            ticker               TEXT NOT NULL,
            strike               REAL,
            expiry               TEXT,
            side                 TEXT,
            sentiment            TEXT,
            premium              REAL,
            size                 INTEGER,
            open_interest        INTEGER,
            vol_oi_ratio         REAL,
            fill_price           REAL,
            flagged_at           TEXT,
            news_context         TEXT,
            claude_reasoning     TEXT,
            catalyst_found       INTEGER,
            catalyst_description TEXT,
            confidence           REAL,
            pristine_alpha       INTEGER
        )
    """)
    conn.commit()
    return conn


def save_sweep(conn: sqlite3.Connection, sweep: dict, analysis: SweepAnalysis) -> None:
    conn.execute("""
        INSERT OR REPLACE INTO sweeps VALUES (
            :id, :ticker, :strike, :expiry, :side, :sentiment,
            :premium, :size, :open_interest, :vol_oi_ratio, :fill_price,
            :flagged_at, :news_context, :claude_reasoning, :catalyst_found,
            :catalyst_description, :confidence, :pristine_alpha
        )
    """, {
        "id": sweep.get("id", ""),
        "ticker": sweep.get("ticker", ""),
        "strike": sweep.get("strike"),
        "expiry": sweep.get("expiry_date"),
        "side": sweep.get("put_call"),
        "sentiment": sweep.get("sentiment"),
        "premium": sweep.get("total_premium"),
        "size": sweep.get("volume"),
        "open_interest": sweep.get("open_interest"),
        "vol_oi_ratio": sweep.get("vol_oi_ratio"),
        "fill_price": sweep.get("price"),
        "flagged_at": datetime.now(timezone.utc).isoformat(),
        "news_context": sweep.get("_news_context", ""),
        "claude_reasoning": analysis.reasoning,
        "catalyst_found": int(analysis.catalyst_found),
        "catalyst_description": analysis.catalyst_description,
        "confidence": analysis.confidence,
        "pristine_alpha": int(analysis.is_pristine_alpha),
    })
    conn.commit()


# ---------------------------------------------------------------------------
# Step 1 -- yfinance options scan (no API key needed)
# ---------------------------------------------------------------------------

def _scan_ticker(ticker_symbol: str, min_premium: float) -> list[dict]:
    try:
        ticker = yf.Ticker(ticker_symbol)
        expirations = ticker.options
        if not expirations:
            return []

        hits = []
        for expiry in expirations[:3]:  # next 3 expiry dates only
            try:
                chain = ticker.option_chain(expiry)
            except Exception:
                continue

            for side, df in [("CALL", chain.calls), ("PUT", chain.puts)]:
                for _, row in df.iterrows():
                    volume = int(row.get("volume") or 0)
                    oi = int(row.get("openInterest") or 0)
                    if volume < 100 or oi < 1:
                        continue

                    vol_oi = volume / oi
                    if vol_oi < 3.0:  # volume must be 3x open interest
                        continue

                    ask = float(row.get("ask") or 0)
                    bid = float(row.get("bid") or 0)
                    mid = (ask + bid) / 2 if (ask and bid) else (ask or bid)
                    estimated_premium = mid * volume * 100

                    if estimated_premium < min_premium:
                        continue

                    hits.append({
                        "id": str(row.get("contractSymbol",
                                          f"{ticker_symbol}_{expiry}_{row['strike']}_{side}")),
                        "ticker": ticker_symbol,
                        "put_call": side,
                        "strike": float(row["strike"]),
                        "expiry_date": expiry,
                        "total_premium": round(estimated_premium, 2),
                        "volume": volume,
                        "open_interest": oi,
                        "vol_oi_ratio": round(vol_oi, 2),
                        "price": round(mid, 4),
                        "implied_volatility": round(float(row.get("impliedVolatility") or 0), 4),
                        "in_the_money": bool(row.get("inTheMoney", False)),
                        "sentiment": "BULLISH" if side == "CALL" else "BEARISH",
                    })
        return hits

    except Exception as exc:
        print(f"  [{ticker_symbol}] skipped: {exc}")
        return []


async def fetch_unusual_options(
    tickers: list[str],
    min_premium: float,
    top_n: int = 30,
) -> list[dict]:
    loop = asyncio.get_event_loop()
    print(f"Scanning {len(tickers)} tickers for unusual options activity...", flush=True)

    with ThreadPoolExecutor(max_workers=12) as pool:
        tasks = [
            loop.run_in_executor(pool, _scan_ticker, t, min_premium)
            for t in tickers
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_hits: list[dict] = []
    for r in results:
        if isinstance(r, list):
            all_hits.extend(r)

    all_hits.sort(key=lambda x: x["total_premium"], reverse=True)
    return all_hits[:top_n]


# ---------------------------------------------------------------------------
# Step 2 -- Perplexity news context
# ---------------------------------------------------------------------------

async def get_news_context(
    client: httpx.AsyncClient,
    perplexity_key: str,
    ticker: str,
    strike: float | None,
    expiry: str | None,
) -> str:
    strike_str = f" ${strike}" if strike else ""
    expiry_str = f" expiring {expiry}" if expiry else ""
    query = (
        f"Is there any recent news, earnings announcement, FDA event, SEC filing, "
        f"M&A rumor, or analyst action for {ticker}{strike_str} options{expiry_str}? "
        f"Focus on events in the last 30 days that could explain unusual options activity."
    )
    try:
        resp = await client.post(
            PERPLEXITY_URL,
            json={"model": "sonar", "messages": [{"role": "user", "content": query}], "max_tokens": 512},
            headers={"Authorization": f"Bearer {perplexity_key}", "Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        return f"[news lookup failed: {exc}]"


# ---------------------------------------------------------------------------
# Step 3 -- Claude structured reasoning
# ---------------------------------------------------------------------------

async def analyze_sweep(claude: AsyncAnthropic, sweep: dict, news_context: str) -> SweepAnalysis:
    ticker = sweep.get("ticker", "UNKNOWN")
    premium = sweep.get("total_premium", 0)
    side = sweep.get("put_call", "?")
    strike = sweep.get("strike", "?")
    expiry = sweep.get("expiry_date", "?")
    volume = sweep.get("volume", "?")
    oi = sweep.get("open_interest", "?")
    vol_oi = sweep.get("vol_oi_ratio", "?")
    sentiment = sweep.get("sentiment", "?")
    itm = "ITM" if sweep.get("in_the_money") else "OTM"

    msg = await claude.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        thinking={"type": "adaptive"},
        system=(
            "You are an expert options flow analyst. "
            "Evaluate whether unusual options activity represents informed trading with no public catalyst. "
            "Be skeptical — high volume/OI ratios often have mundane explanations (earnings plays, hedges). "
            "Only flag as Pristine Alpha when estimated premium is large AND news search returns nothing credible."
        ),
        output_config={
            "format": {
                "type": "json_schema",
                "name": "sweep_analysis",
                "schema": ANALYSIS_SCHEMA,
                "strict": True,
            }
        },
        messages=[{"role": "user", "content": f"""
Analyze this unusual options contract for "Pristine Alpha":

CONTRACT:
- Ticker: {ticker} | {itm}
- Side: {side} | Sentiment: {sentiment}
- Strike: ${strike} | Expiry: {expiry}
- Estimated Premium: ${float(premium):,.0f}
- Volume: {volume:,} | Open Interest: {oi:,} | Vol/OI Ratio: {vol_oi}x

NEWS CONTEXT (via Perplexity real-time search):
{news_context}

Is this Pristine Alpha -- large estimated premium with NO identifiable public catalyst?
Note: premium is estimated from mid-price x volume x 100 (not confirmed execution price).
""".strip()}],
    )

    text = next((b.text for b in msg.content if b.type == "text" and b.text), "{}")
    return SweepAnalysis(**json.loads(text))


# ---------------------------------------------------------------------------
# Email report
# ---------------------------------------------------------------------------

def write_email_report(
    results: list[tuple[dict, SweepAnalysis]],
    min_premium: float,
    scan_time: datetime,
    tickers_scanned: int,
) -> None:
    pristine = [(s, a) for s, a in results if a.is_pristine_alpha]
    others = [(s, a) for s, a in results if not a.is_pristine_alpha]

    def side_color(side: str) -> str:
        return "#16a34a" if "CALL" in side.upper() else "#dc2626"

    def sentiment_badge(sentiment: str) -> str:
        s = (sentiment or "").upper()
        color = "#16a34a" if "BULL" in s else ("#dc2626" if "BEAR" in s else "#6b7280")
        return (f'<span style="background:{color};color:#fff;padding:2px 8px;'
                f'border-radius:4px;font-size:12px;">{sentiment or "?"}</span>')

    def sweep_row(sweep: dict, analysis: SweepAnalysis, highlight: bool) -> str:
        premium = float(sweep.get("total_premium") or 0)
        volume = sweep.get("volume", 0)
        oi = sweep.get("open_interest", 0)
        vol_oi = sweep.get("vol_oi_ratio", 0)
        side = sweep.get("put_call", "?")
        bg = "#fefce8" if highlight else "#ffffff"
        border = "border-left:4px solid #eab308;" if highlight else ""
        return (
            f'<tr style="background:{bg};{border}">'
            f'<td style="padding:10px 12px;font-weight:700;color:#1e293b;">{sweep.get("ticker","?")}</td>'
            f'<td style="padding:10px 12px;color:{side_color(side)};font-weight:600;">{side}</td>'
            f'<td style="padding:10px 12px;">${sweep.get("strike","?")}</td>'
            f'<td style="padding:10px 12px;">{sweep.get("expiry_date","?")}</td>'
            f'<td style="padding:10px 12px;font-weight:600;">${premium:,.0f}</td>'
            f'<td style="padding:10px 12px;">{volume:,}</td>'
            f'<td style="padding:10px 12px;">{oi:,}</td>'
            f'<td style="padding:10px 12px;font-weight:600;">{vol_oi}x</td>'
            f'<td style="padding:10px 12px;">{sentiment_badge(sweep.get("sentiment",""))}</td>'
            f'<td style="padding:10px 12px;color:#6b7280;font-size:13px;max-width:240px;">'
            f'{analysis.reasoning[:140]}</td>'
            f'<td style="padding:10px 12px;text-align:center;font-weight:700;">'
            f'{analysis.confidence:.0%}</td>'
            f'</tr>'
        )

    thead = (
        '<thead><tr style="background:{color};">'
        '<th style="padding:10px 12px;text-align:left;">Ticker</th>'
        '<th style="padding:10px 12px;text-align:left;">Side</th>'
        '<th style="padding:10px 12px;text-align:left;">Strike</th>'
        '<th style="padding:10px 12px;text-align:left;">Expiry</th>'
        '<th style="padding:10px 12px;text-align:left;">Est. Premium</th>'
        '<th style="padding:10px 12px;text-align:left;">Volume</th>'
        '<th style="padding:10px 12px;text-align:left;">OI</th>'
        '<th style="padding:10px 12px;text-align:left;">Vol/OI</th>'
        '<th style="padding:10px 12px;text-align:left;">Sentiment</th>'
        '<th style="padding:10px 12px;text-align:left;">Claude Reasoning</th>'
        '<th style="padding:10px 12px;text-align:center;">Conf.</th>'
        '</tr></thead>'
    )

    def table(rows_html: str, color: str) -> str:
        hdr = thead.replace("{color}", color)
        return (
            '<div style="border:1px solid #e2e8f0;border-radius:8px;overflow-x:auto;margin-bottom:24px;">'
            '<table width="100%" cellpadding="0" cellspacing="0" '
            'style="border-collapse:collapse;font-family:sans-serif;font-size:13px;">'
            f'{hdr}<tbody>{rows_html}</tbody></table></div>'
        )

    scan_dt = scan_time.strftime("%A, %B %d %Y at %H:%M UTC")
    no_flags = "" if pristine else (
        '<div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;'
        'padding:20px;text-align:center;color:#15803d;margin-bottom:24px;">'
        '<strong>No Pristine Alpha signals today.</strong> '
        'All high-volume contracts had identifiable catalysts.</div>'
    )
    pristine_section = (
        f'<h2 style="color:#92400e;margin:0 0 12px;">&#11088; Pristine Alpha Flags ({len(pristine)})</h2>'
        + table("".join(sweep_row(s, a, True) for s, a in pristine), "#fbbf24")
    ) if pristine else ""
    other_section = (
        f'<h2 style="color:#475569;margin:0 0 12px;">All Other Hits Analyzed ({len(others)})</h2>'
        + table("".join(sweep_row(s, a, False) for s, a in others), "#f1f5f9")
    ) if others else ""

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f8fafc;font-family:system-ui,sans-serif;">
<div style="max-width:980px;margin:32px auto;background:#fff;border-radius:12px;
     overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.08);">
  <div style="background:linear-gradient(135deg,#0f172a,#1e3a5f);padding:28px 32px;">
    <h1 style="margin:0;color:#f8fafc;font-size:22px;">&#128200; Pristine Alpha Hunter</h1>
    <p style="margin:6px 0 0;color:#94a3b8;font-size:14px;">{scan_dt} &bull; yfinance data source</p>
  </div>
  <div style="display:flex;border-bottom:1px solid #e2e8f0;">
    <div style="flex:1;padding:20px 24px;text-align:center;border-right:1px solid #e2e8f0;">
      <div style="font-size:28px;font-weight:700;color:#1e293b;">{tickers_scanned}</div>
      <div style="font-size:12px;color:#64748b;margin-top:4px;">TICKERS SCANNED</div>
    </div>
    <div style="flex:1;padding:20px 24px;text-align:center;border-right:1px solid #e2e8f0;">
      <div style="font-size:28px;font-weight:700;color:#1e293b;">{len(results)}</div>
      <div style="font-size:12px;color:#64748b;margin-top:4px;">UNUSUAL CONTRACTS</div>
    </div>
    <div style="flex:1;padding:20px 24px;text-align:center;border-right:1px solid #e2e8f0;">
      <div style="font-size:28px;font-weight:700;color:#d97706;">{len(pristine)}</div>
      <div style="font-size:12px;color:#64748b;margin-top:4px;">PRISTINE ALPHA FLAGS</div>
    </div>
    <div style="flex:1;padding:20px 24px;text-align:center;">
      <div style="font-size:28px;font-weight:700;color:#1e293b;">${min_premium/1e3:.0f}K+</div>
      <div style="font-size:12px;color:#64748b;margin-top:4px;">PREMIUM THRESHOLD</div>
    </div>
  </div>
  <div style="padding:28px 32px;">
    {no_flags}{pristine_section}{other_section}
  </div>
  <div style="background:#f1f5f9;padding:16px 32px;border-top:1px solid #e2e8f0;">
    <p style="margin:0;font-size:12px;color:#94a3b8;">
      Data: Yahoo Finance (free, no API key) &bull; Context: Perplexity Sonar
      &bull; Analysis: Claude Opus 4.7<br>
      Est. premium = mid-price &times; volume &times; 100. Not financial advice.
    </p>
  </div>
</div>
</body></html>"""

    with open("email_summary.html", "w", encoding="utf-8") as f:
        f.write(html)
    with open("scan_meta.env", "w") as f:
        f.write(f"FLAG_COUNT={len(pristine)}\n")
        f.write(f"SWEEP_COUNT={len(results)}\n")
        f.write(f"SCAN_DATE={scan_time.strftime('%Y-%m-%d')}\n")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def run_scan(min_premium: float, db_path: str, tickers: list[str]) -> None:
    perplexity_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if not perplexity_key:
        print("Error: PERPLEXITY_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)

    conn = init_db(db_path)
    claude = AsyncAnthropic()
    scan_time = datetime.now(timezone.utc)
    results: list[tuple[dict, SweepAnalysis]] = []

    sweeps = await fetch_unusual_options(tickers, min_premium)
    print(f"Found {len(sweeps)} unusual contracts above ${min_premium:,.0f} est. premium\n")

    if not sweeps:
        print("Nothing matched. Try lowering --min-premium.")
        write_email_report([], min_premium, scan_time, len(tickers))
        conn.close()
        return

    async with httpx.AsyncClient() as http:
        for i, sweep in enumerate(sweeps, 1):
            ticker = sweep["ticker"]
            side = sweep["put_call"]
            strike = sweep["strike"]
            expiry = sweep["expiry_date"]
            premium = sweep["total_premium"]
            vol_oi = sweep["vol_oi_ratio"]

            print(f"[{i}/{len(sweeps)}] {ticker} {side} ${strike} exp {expiry} "
                  f"-- est. ${premium:,.0f} | Vol/OI {vol_oi}x")

            print("  -> Perplexity...", end=" ", flush=True)
            news = await get_news_context(http, perplexity_key, ticker, strike, expiry)
            sweep["_news_context"] = news
            print(f'"{news[:70].replace(chr(10), " ")}..."')

            print("  -> Claude...", end=" ", flush=True)
            analysis = await analyze_sweep(claude, sweep, news)
            flag = "*** PRISTINE ALPHA ***" if analysis.is_pristine_alpha else "  no flag"
            print(f"{flag} ({analysis.confidence:.0%})")

            save_sweep(conn, sweep, analysis)
            results.append((sweep, analysis))

    conn.close()
    write_email_report(results, min_premium, scan_time, len(tickers))
    pristine_count = sum(1 for _, a in results if a.is_pristine_alpha)
    print(f"\nDone. {pristine_count}/{len(results)} Pristine Alpha flags.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Pristine Alpha Hunter (yfinance)")
    parser.add_argument("--min-premium", type=float, default=500_000, metavar="DOLLARS")
    parser.add_argument("--db", default="alpha_sweeps.db", metavar="PATH")
    parser.add_argument("--tickers", nargs="*", default=None,
                        help="Custom ticker list (default: built-in ~50 liquid names)")
    args = parser.parse_args()
    tickers = args.tickers if args.tickers else DEFAULT_TICKERS
    asyncio.run(run_scan(args.min_premium, args.db, tickers))


if __name__ == "__main__":
    main()
