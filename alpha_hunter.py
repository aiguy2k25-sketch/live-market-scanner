"""
Pristine Alpha Hunter — institutional sweep scanner.

Pulls large options sweeps (>$500k) from Unusual Whales, checks each against
Perplexity for real-time news context, then has Claude reason about whether
the flow looks like informed money with no public catalyst ("Pristine Alpha").

Environment variables:
    ANTHROPIC_API_KEY   — your Anthropic key
    UW_API_TOKEN        — your Unusual Whales API token
    PERPLEXITY_API_KEY  — your Perplexity API key
"""
import argparse
import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

import httpx
from anthropic import AsyncAnthropic
from pydantic import BaseModel

UW_BASE = "https://api.unusualwhales.com"
PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"
MODEL = "claude-opus-4-7"
MAX_TOKENS = 2048


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
            :premium, :size, :fill_price, :flagged_at,
            :news_context, :claude_reasoning, :catalyst_found,
            :catalyst_description, :confidence, :pristine_alpha
        )
    """, {
        "id": sweep.get("id", ""),
        "ticker": sweep.get("ticker", ""),
        "strike": sweep.get("strike"),
        "expiry": sweep.get("expiry_date") or sweep.get("expiry"),
        "side": sweep.get("put_call") or sweep.get("side"),
        "sentiment": sweep.get("sentiment"),
        "premium": sweep.get("total_premium") or sweep.get("premium"),
        "size": sweep.get("size") or sweep.get("volume"),
        "fill_price": sweep.get("price") or sweep.get("fill_price"),
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
# Step 1 — Unusual Whales sweep fetch
# ---------------------------------------------------------------------------

async def fetch_sweeps(
    client: httpx.AsyncClient,
    uw_token: str,
    min_premium: float = 500_000,
) -> list[dict]:
    headers = {"Authorization": f"Bearer {uw_token}", "Accept": "application/json"}
    resp = await client.get(
        f"{UW_BASE}/api/option-trades/flow-alerts",
        headers=headers,
        params={"limit": 100, "order": "desc"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    records = data.get("data", data) if isinstance(data, dict) else data

    sweeps = []
    for trade in records:
        trade_type = (trade.get("type") or trade.get("alert_type") or "").upper()
        if trade_type and trade_type != "SWEEP":
            continue
        premium = float(trade.get("total_premium") or trade.get("premium") or 0)
        if premium >= min_premium:
            sweeps.append(trade)
    return sweeps


# ---------------------------------------------------------------------------
# Step 2 — Perplexity news context
# ---------------------------------------------------------------------------

async def get_news_context(
    client: httpx.AsyncClient,
    perplexity_key: str,
    ticker: str,
    strike: str | float | None,
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
# Step 3 — Claude structured reasoning
# ---------------------------------------------------------------------------

async def analyze_sweep(claude: AsyncAnthropic, sweep: dict, news_context: str) -> SweepAnalysis:
    ticker = sweep.get("ticker", "UNKNOWN")
    premium = sweep.get("total_premium") or sweep.get("premium", 0)
    side = sweep.get("put_call") or sweep.get("side", "unknown")
    strike = sweep.get("strike", "?")
    expiry = sweep.get("expiry_date") or sweep.get("expiry", "?")
    size = sweep.get("size") or sweep.get("volume", "?")
    sentiment = sweep.get("sentiment", "unknown")

    msg = await claude.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        thinking={"type": "adaptive"},
        system=(
            "You are an expert options flow analyst. "
            "Evaluate whether institutional sweeps represent informed trading with no public catalyst. "
            "Be skeptical — most unusual activity has a mundane explanation. "
            "Only flag as Pristine Alpha when premium is genuinely large (>$1M preferred) "
            "AND the Perplexity search returns no credible catalyst."
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
Analyze this institutional options sweep for "Pristine Alpha":

TRADE:
- Ticker: {ticker}
- Side: {side.upper()} | Sentiment: {sentiment}
- Strike: ${strike} | Expiry: {expiry}
- Premium: ${float(premium or 0):,.0f} | Contracts: {size}

NEWS CONTEXT (via Perplexity real-time search):
{news_context}

Is this Pristine Alpha — large premium sweep with NO identifiable public catalyst?
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
) -> None:
    pristine = [(s, a) for s, a in results if a.is_pristine_alpha]
    others = [(s, a) for s, a in results if not a.is_pristine_alpha]

    def side_color(side: str) -> str:
        s = (side or "").upper()
        if "CALL" in s or s == "C":
            return "#16a34a"
        if "PUT" in s or s == "P":
            return "#dc2626"
        return "#6b7280"

    def sentiment_badge(sentiment: str) -> str:
        s = (sentiment or "").upper()
        color = "#16a34a" if "BULL" in s else ("#dc2626" if "BEAR" in s else "#6b7280")
        return f'<span style="background:{color};color:#fff;padding:2px 8px;border-radius:4px;font-size:12px;">{sentiment or "—"}</span>'

    def sweep_row(sweep: dict, analysis: SweepAnalysis, highlight: bool) -> str:
        ticker = sweep.get("ticker", "?")
        premium = float(sweep.get("total_premium") or sweep.get("premium") or 0)
        side = sweep.get("put_call") or sweep.get("side") or "?"
        strike = sweep.get("strike", "?")
        expiry = sweep.get("expiry_date") or sweep.get("expiry", "?")
        size = sweep.get("size") or sweep.get("volume", "?")
        sentiment = sweep.get("sentiment", "")
        bg = "#fefce8" if highlight else "#ffffff"
        border = "border-left: 4px solid #eab308;" if highlight else ""
        return f"""
        <tr style="background:{bg};{border}">
          <td style="padding:10px 12px;font-weight:700;font-size:15px;color:#1e293b;">{ticker}</td>
          <td style="padding:10px 12px;color:{side_color(side)};font-weight:600;">{side.upper()}</td>
          <td style="padding:10px 12px;">${strike}</td>
          <td style="padding:10px 12px;">{expiry}</td>
          <td style="padding:10px 12px;font-weight:600;">${premium:,.0f}</td>
          <td style="padding:10px 12px;">{size:,}</td>
          <td style="padding:10px 12px;">{sentiment_badge(sentiment)}</td>
          <td style="padding:10px 12px;color:#6b7280;font-size:13px;max-width:280px;">{analysis.reasoning[:160]}</td>
          <td style="padding:10px 12px;text-align:center;font-weight:700;">{analysis.confidence:.0%}</td>
        </tr>"""

    pristine_rows = "".join(sweep_row(s, a, True) for s, a in pristine)
    other_rows = "".join(sweep_row(s, a, False) for s, a in others)

    no_flags_msg = ""
    if not pristine:
        no_flags_msg = """
        <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:20px;text-align:center;color:#15803d;margin-bottom:24px;">
          <strong>No Pristine Alpha signals today.</strong> All large sweeps had identifiable catalysts or insufficient premium.
        </div>"""

    pristine_section = ""
    if pristine:
        pristine_section = f"""
        <h2 style="color:#92400e;margin:0 0 12px;">&#11088; Pristine Alpha Flags ({len(pristine)})</h2>
        <div style="background:#fef3c7;border:1px solid #fcd34d;border-radius:8px;padding:4px;margin-bottom:24px;overflow-x:auto;">
          <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-family:sans-serif;font-size:14px;">
            <thead>
              <tr style="background:#fbbf24;">
                <th style="padding:10px 12px;text-align:left;">Ticker</th>
                <th style="padding:10px 12px;text-align:left;">Side</th>
                <th style="padding:10px 12px;text-align:left;">Strike</th>
                <th style="padding:10px 12px;text-align:left;">Expiry</th>
                <th style="padding:10px 12px;text-align:left;">Premium</th>
                <th style="padding:10px 12px;text-align:left;">Size</th>
                <th style="padding:10px 12px;text-align:left;">Sentiment</th>
                <th style="padding:10px 12px;text-align:left;">Claude Reasoning</th>
                <th style="padding:10px 12px;text-align:center;">Conf.</th>
              </tr>
            </thead>
            <tbody>{pristine_rows}</tbody>
          </table>
        </div>"""

    other_section = ""
    if others:
        other_section = f"""
        <h2 style="color:#475569;margin:0 0 12px;">All Other Sweeps Scanned ({len(others)})</h2>
        <div style="border:1px solid #e2e8f0;border-radius:8px;padding:4px;overflow-x:auto;">
          <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-family:sans-serif;font-size:13px;">
            <thead>
              <tr style="background:#f1f5f9;">
                <th style="padding:8px 12px;text-align:left;">Ticker</th>
                <th style="padding:8px 12px;text-align:left;">Side</th>
                <th style="padding:8px 12px;text-align:left;">Strike</th>
                <th style="padding:8px 12px;text-align:left;">Expiry</th>
                <th style="padding:8px 12px;text-align:left;">Premium</th>
                <th style="padding:8px 12px;text-align:left;">Size</th>
                <th style="padding:8px 12px;text-align:left;">Sentiment</th>
                <th style="padding:8px 12px;text-align:left;">Claude Reasoning</th>
                <th style="padding:8px 12px;text-align:center;">Conf.</th>
              </tr>
            </thead>
            <tbody>{other_rows}</tbody>
          </table>
        </div>"""

    scan_dt = scan_time.strftime("%A, %B %-d %Y at %-I:%M %p CST")
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f8fafc;font-family:system-ui,sans-serif;">
  <div style="max-width:900px;margin:32px auto;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.08);">

    <!-- Header -->
    <div style="background:linear-gradient(135deg,#0f172a 0%,#1e3a5f 100%);padding:28px 32px;">
      <h1 style="margin:0;color:#f8fafc;font-size:22px;font-weight:700;">&#128200; Pristine Alpha Hunter</h1>
      <p style="margin:6px 0 0;color:#94a3b8;font-size:14px;">{scan_dt}</p>
    </div>

    <!-- Summary bar -->
    <div style="display:flex;gap:0;border-bottom:1px solid #e2e8f0;">
      <div style="flex:1;padding:20px 24px;text-align:center;border-right:1px solid #e2e8f0;">
        <div style="font-size:28px;font-weight:700;color:#1e293b;">{len(results)}</div>
        <div style="font-size:12px;color:#64748b;margin-top:4px;">SWEEPS SCANNED</div>
      </div>
      <div style="flex:1;padding:20px 24px;text-align:center;border-right:1px solid #e2e8f0;">
        <div style="font-size:28px;font-weight:700;color:#d97706;">{len(pristine)}</div>
        <div style="font-size:12px;color:#64748b;margin-top:4px;">PRISTINE ALPHA FLAGS</div>
      </div>
      <div style="flex:1;padding:20px 24px;text-align:center;">
        <div style="font-size:28px;font-weight:700;color:#1e293b;">${min_premium/1e6:.1f}M+</div>
        <div style="font-size:12px;color:#64748b;margin-top:4px;">PREMIUM THRESHOLD</div>
      </div>
    </div>

    <!-- Body -->
    <div style="padding:28px 32px;">
      {no_flags_msg}
      {pristine_section}
      {other_section}
    </div>

    <!-- Footer -->
    <div style="background:#f1f5f9;padding:16px 32px;border-top:1px solid #e2e8f0;">
      <p style="margin:0;font-size:12px;color:#94a3b8;">
        Powered by Unusual Whales &bull; Perplexity Sonar &bull; Claude Opus 4.7 &bull;
        Not financial advice. Do your own due diligence.
      </p>
    </div>

  </div>
</body>
</html>"""

    with open("email_summary.html", "w", encoding="utf-8") as f:
        f.write(html)

    # Write metadata so the workflow can set the email subject
    with open("scan_meta.env", "w") as f:
        f.write(f"FLAG_COUNT={len(pristine)}\n")
        f.write(f"SWEEP_COUNT={len(results)}\n")
        f.write(f"SCAN_DATE={scan_time.strftime('%Y-%m-%d')}\n")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def run_scan(min_premium: float, db_path: str) -> None:
    uw_token = os.environ.get("UW_API_TOKEN", "").strip()
    perplexity_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if not uw_token:
        print("Error: UW_API_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)
    if not perplexity_key:
        print("Error: PERPLEXITY_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)

    conn = init_db(db_path)
    claude = AsyncAnthropic()
    scan_time = datetime.now(timezone.utc)
    results: list[tuple[dict, SweepAnalysis]] = []

    async with httpx.AsyncClient() as http:
        print(f"Fetching sweeps >= ${min_premium:,.0f}...", end=" ", flush=True)
        sweeps = await fetch_sweeps(http, uw_token, min_premium)
        print(f"{len(sweeps)} found")

        if not sweeps:
            print("No sweeps matched the filter.")
            write_email_report([], min_premium, scan_time)
            conn.close()
            return

        for i, sweep in enumerate(sweeps, 1):
            ticker = sweep.get("ticker", "?")
            premium = float(sweep.get("total_premium") or sweep.get("premium") or 0)
            side = (sweep.get("put_call") or sweep.get("side") or "?").upper()
            strike = sweep.get("strike", "?")
            expiry = sweep.get("expiry_date") or sweep.get("expiry", "?")

            print(f"\n[{i}/{len(sweeps)}] {ticker} {side} ${strike} exp {expiry} -- ${premium:,.0f}")

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
    write_email_report(results, min_premium, scan_time)

    pristine_count = sum(1 for _, a in results if a.is_pristine_alpha)
    print(f"\nScan complete. {pristine_count}/{len(results)} Pristine Alpha flags.")
    print(f"DB: {db_path} | Email: email_summary.html")


def main() -> None:
    parser = argparse.ArgumentParser(description="Pristine Alpha Hunter")
    parser.add_argument("--min-premium", type=float, default=500_000, metavar="DOLLARS")
    parser.add_argument("--db", default="alpha_sweeps.db", metavar="PATH")
    args = parser.parse_args()
    asyncio.run(run_scan(args.min_premium, args.db))


if __name__ == "__main__":
    main()
