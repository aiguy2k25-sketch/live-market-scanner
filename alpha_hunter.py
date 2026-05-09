"""
Pristine Alpha Hunter — institutional sweep scanner.

Pulls large options sweeps (>$500k) from Unusual Whales, checks each against
Perplexity for real-time news context, then has Claude reason about whether
the flow looks like informed money with no public catalyst ("Pristine Alpha").

Environment variables:
    ANTHROPIC_API_KEY   — your Anthropic key
    UW_API_TOKEN        — your Unusual Whales API token
    PERPLEXITY_API_KEY  — your Perplexity API key

Run locally:
    pip install -r requirements.txt
    python alpha_hunter.py
    python alpha_hunter.py --min-premium 1000000 --db alpha.db
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
    confidence: float          # 0.0–1.0
    catalyst_found: bool
    catalyst_description: str  # empty string if none found
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
        "is_pristine_alpha",
        "confidence",
        "catalyst_found",
        "catalyst_description",
        "reasoning"
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
    headers = {
        "Authorization": f"Bearer {uw_token}",
        "Accept": "application/json",
    }
    params = {
        "limit": 100,
        "order": "desc",
    }

    resp = await client.get(
        f"{UW_BASE}/api/option-trades/flow-alerts",
        headers=headers,
        params=params,
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

    payload = {
        "model": "sonar",
        "messages": [{"role": "user", "content": query}],
        "max_tokens": 512,
    }
    headers = {
        "Authorization": f"Bearer {perplexity_key}",
        "Content-Type": "application/json",
    }

    try:
        resp = await client.post(
            PERPLEXITY_URL,
            json=payload,
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        return result["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        return f"[news lookup failed: {exc}]"


# ---------------------------------------------------------------------------
# Step 3 — Claude structured reasoning
# ---------------------------------------------------------------------------

async def analyze_sweep(
    claude: AsyncAnthropic,
    sweep: dict,
    news_context: str,
) -> SweepAnalysis:
    ticker = sweep.get("ticker", "UNKNOWN")
    premium = sweep.get("total_premium") or sweep.get("premium", 0)
    side = sweep.get("put_call") or sweep.get("side", "unknown")
    strike = sweep.get("strike", "?")
    expiry = sweep.get("expiry_date") or sweep.get("expiry", "?")
    size = sweep.get("size") or sweep.get("volume", "?")
    sentiment = sweep.get("sentiment", "unknown")

    user_message = f"""
Analyze this institutional options sweep for "Pristine Alpha" — large informed money with no public catalyst:

TRADE:
- Ticker: {ticker}
- Side: {side.upper()} | Sentiment: {sentiment}
- Strike: ${strike} | Expiry: {expiry}
- Premium: ${float(premium or 0):,.0f} | Contracts: {size}

NEWS CONTEXT (via Perplexity real-time search):
{news_context}

Determine:
1. Is there a credible public catalyst that could explain this trade?
2. Is this "Pristine Alpha" — large premium sweep with NO identifiable public catalyst?
3. Your confidence level (0.0–1.0) and reasoning.
""".strip()

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
        messages=[{"role": "user", "content": user_message}],
    )

    text = next(
        (b.text for b in msg.content if b.type == "text" and b.text),
        "{}",
    )
    return SweepAnalysis(**json.loads(text))


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

    async with httpx.AsyncClient() as http:
        print(f"Fetching sweeps >= ${min_premium:,.0f}...", end=" ", flush=True)
        sweeps = await fetch_sweeps(http, uw_token, min_premium)
        print(f"{len(sweeps)} found")

        if not sweeps:
            print("No sweeps matched the filter. Try lowering --min-premium.")
            conn.close()
            return

        pristine_count = 0

        for i, sweep in enumerate(sweeps, 1):
            ticker = sweep.get("ticker", "?")
            premium = float(sweep.get("total_premium") or sweep.get("premium") or 0)
            side = (sweep.get("put_call") or sweep.get("side") or "?").upper()
            strike = sweep.get("strike", "?")
            expiry = sweep.get("expiry_date") or sweep.get("expiry", "?")

            print(f"\n[{i}/{len(sweeps)}] {ticker} {side} ${strike} exp {expiry} -- ${premium:,.0f}")

            print("  -> Perplexity news check...", end=" ", flush=True)
            news = await get_news_context(http, perplexity_key, ticker, strike, expiry)
            sweep["_news_context"] = news
            preview = news[:80].replace("\n", " ")
            print(f'"{preview}..."')

            print("  -> Claude analysis...", end=" ", flush=True)
            analysis = await analyze_sweep(claude, sweep, news)

            flag = "*** PRISTINE ALPHA ***" if analysis.is_pristine_alpha else "  no flag"
            print(f"{flag} (confidence: {analysis.confidence:.0%})")
            print(f"     {analysis.reasoning[:120]}")

            save_sweep(conn, sweep, analysis)

            if analysis.is_pristine_alpha:
                pristine_count += 1

    conn.close()

    print(f"\n{'─' * 60}")
    print(f"Scan complete. {pristine_count}/{len(sweeps)} sweeps flagged as Pristine Alpha.")
    print(f"Results saved to: {db_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pristine Alpha Hunter -- scan institutional sweeps for informed flow"
    )
    parser.add_argument(
        "--min-premium",
        type=float,
        default=500_000,
        metavar="DOLLARS",
        help="Minimum total premium to consider (default: 500000)",
    )
    parser.add_argument(
        "--db",
        default="alpha_sweeps.db",
        metavar="PATH",
        help="SQLite database path (default: alpha_sweeps.db)",
    )
    args = parser.parse_args()
    asyncio.run(run_scan(args.min_premium, args.db))


if __name__ == "__main__":
    main()
