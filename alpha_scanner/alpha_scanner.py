"""
Alpha Scanner
Fetches top 10 pre-market gainers + news catalysts from Alpha Vantage
and emails results via Gmail SMTP at 8 AM CST weekdays.
"""

import os
import smtplib
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

# ── Config ────────────────────────────────────────────────────────────────────

AV_KEY        = os.environ.get("ALPHA_VANTAGE_KEY", "NKEQJ0HERGZIQL0S")
SMTP_HOST     = os.environ.get("SMTP_HOST",  "smtp.gmail.com")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER",  "")
SMTP_PASS     = os.environ.get("SMTP_PASS",  "")
EMAIL_TO      = os.environ.get("EMAIL_TO",   "2daysale@gmail.com")
TOP_N         = 10
AV_BASE       = "https://www.alphavantage.co/query"

# ── Alpha Vantage helpers ─────────────────────────────────────────────────────

def av_get(params: dict) -> dict:
    params["apikey"] = AV_KEY
    resp = requests.get(AV_BASE, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if "Error Message" in data:
        raise ValueError(f"Alpha Vantage error: {data['Error Message']}")
    if "Note" in data:
        print(f"[RATE LIMIT] {data['Note']}")
    return data


def fetch_top_gainers() -> list[dict]:
    data = av_get({"function": "TOP_GAINERS_LOSERS"})
    gainers = data.get("top_gainers", [])[:TOP_N]
    if not gainers:
        raise ValueError(f"No top_gainers in response: {str(data)[:200]}")
    return gainers


def fetch_news(tickers: list[str]) -> dict[str, list[dict]]:
    """Fetch news for all tickers in one API call, grouped by ticker."""
    data = av_get({
        "function": "NEWS_SENTIMENT",
        "tickers": ",".join(tickers),
        "limit": 60,
        "sort": "RELEVANCE",
    })

    by_ticker: dict[str, list[dict]] = {t: [] for t in tickers}
    for article in data.get("feed", []):
        for ts in article.get("ticker_sentiment", []):
            t = ts.get("ticker", "")
            if t in by_ticker and len(by_ticker[t]) < 3:
                by_ticker[t].append({
                    "title":     article.get("title", ""),
                    "url":       article.get("url", ""),
                    "summary":   article.get("summary", ""),
                    "source":    article.get("source", ""),
                    "published": article.get("time_published", ""),
                    "sentiment": float(ts.get("ticker_sentiment_score", 0)),
                })
    return by_ticker


def fmt_av_time(ts: str) -> str:
    """Convert Alpha Vantage timestamp (20240115T143000) to readable string."""
    if not ts or len(ts) < 15:
        return ""
    try:
        from datetime import datetime
        dt = datetime(
            int(ts[0:4]), int(ts[4:6]), int(ts[6:8]),
            int(ts[9:11]), int(ts[11:13]),
            tzinfo=timezone.utc
        )
        return dt.strftime("%-m/%-d %-I:%M %p UTC")
    except Exception:
        return ""


# ── Email builder ─────────────────────────────────────────────────────────────

def build_html(gainers: list[dict], news_map: dict[str, list[dict]]) -> str:
    date_str = datetime.now().strftime("%A, %B %-d, %Y")

    cards = ""
    for i, s in enumerate(gainers):
        ticker  = s.get("ticker", "")
        price   = s.get("price", "")
        change  = s.get("change_amount", "")
        pct     = s.get("change_percentage", "")
        volume  = int(s.get("volume", 0) or 0)

        try:
            pct_val = float(pct.replace("%", "").replace("+", ""))
        except Exception:
            pct_val = 0
        badge_color = "#b71c1c" if pct_val >= 20 else "#c62828" if pct_val >= 10 else "#2e7d32"

        articles = news_map.get(ticker, [])
        if articles:
            news_html = ""
            for n in articles:
                sent = n["sentiment"]
                sentiment_label = "🟢 Bullish" if sent > 0.15 else "🔴 Bearish" if sent < -0.15 else "⚪ Neutral"
                summary = (n["summary"] or "")[:240]
                if len(n["summary"] or "") > 240:
                    summary += "…"
                news_html += f"""
                <div style="border-left:3px solid #1565c0;padding:6px 0 6px 12px;margin:10px 0;">
                  <a href="{n['url']}" style="color:#1565c0;text-decoration:none;font-weight:600;font-size:13px;line-height:1.4;">{n['title']}</a>
                  <p style="margin:4px 0 2px;color:#555;font-size:12px;line-height:1.5;">{summary}</p>
                  <small style="color:#888;">{n['source']} &nbsp;•&nbsp; {fmt_av_time(n['published'])} &nbsp;•&nbsp; {sentiment_label}</small>
                </div>"""
        else:
            news_html = '<p style="color:#999;font-style:italic;font-size:13px;margin:0;">No recent news found.</p>'

        cards += f"""
        <div style="background:#fff;border:1px solid #e0e0e0;border-radius:10px;padding:18px 20px;margin:14px 0;box-shadow:0 1px 4px rgba(0,0,0,0.06);">
          <div style="display:flex;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:8px;">
            <span style="font-size:22px;font-weight:800;color:#111;">#{i+1} &nbsp;{ticker}</span>
            <span style="background:{badge_color};color:#fff;padding:3px 14px;border-radius:20px;font-size:16px;font-weight:700;">+{pct}</span>
          </div>
          <table style="width:100%;font-size:13px;color:#444;border-collapse:collapse;margin-bottom:14px;">
            <tr>
              <td style="padding:2px 12px 2px 0;white-space:nowrap;"><strong>Price:</strong> ${price}</td>
              <td style="padding:2px 12px;white-space:nowrap;"><strong>Change:</strong> +${change}</td>
              <td style="padding:2px 0;white-space:nowrap;"><strong>Volume:</strong> {volume:,}</td>
            </tr>
          </table>
          <div style="font-size:13px;font-weight:700;color:#333;text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid #eee;padding-bottom:6px;margin-bottom:4px;">
            Why It's Moving
          </div>
          {news_html}
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Alpha Scanner</title>
</head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,sans-serif;">
  <div style="max-width:680px;margin:0 auto;padding:24px 16px;">

    <div style="background:linear-gradient(135deg,#0d47a1 0%,#1976d2 100%);border-radius:12px;padding:28px 24px;text-align:center;margin-bottom:6px;">
      <div style="font-size:32px;margin-bottom:8px;">📈</div>
      <h1 style="color:#fff;margin:0;font-size:26px;font-weight:800;letter-spacing:-0.5px;">Alpha Scanner</h1>
      <p style="color:#90caf9;margin:8px 0 0;font-size:14px;">Top {TOP_N} Pre-Market Movers &nbsp;•&nbsp; {date_str}</p>
    </div>

    <p style="color:#aaa;font-size:11px;text-align:center;margin:10px 0 18px;line-height:1.6;">
      For informational purposes only. Not financial advice.<br>
      Past performance does not guarantee future results. Always do your own research.
    </p>

    {cards}

    <div style="text-align:center;margin-top:28px;padding-top:16px;border-top:1px solid #ddd;">
      <p style="color:#bbb;font-size:11px;margin:0;line-height:1.8;">
        Alpha Scanner &nbsp;•&nbsp; Powered by Alpha Vantage &nbsp;•&nbsp; Delivered at 8:00 AM CST
      </p>
    </div>

  </div>
</body>
</html>"""


def build_text(gainers: list[dict], news_map: dict[str, list[dict]]) -> str:
    date_str = datetime.now().strftime("%A, %B %d, %Y")
    lines = [f"ALPHA SCANNER — {date_str}", "=" * 60, ""]
    for i, s in enumerate(gainers):
        ticker = s.get("ticker", "")
        lines.append(f"#{i+1} {ticker}  +{s.get('change_percentage','')}  ${s.get('price','')}  Vol: {int(s.get('volume',0) or 0):,}")
        for n in news_map.get(ticker, []):
            lines.append(f"  • {n['title']}")
            lines.append(f"    {n['url']}")
        lines.append("")
    lines.append("Data: Alpha Vantage | Not financial advice")
    return "\n".join(lines)


# ── Email sender ──────────────────────────────────────────────────────────────

def send_email(subject: str, html: str, text: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())

    print(f"[EMAIL] Sent to {EMAIL_TO}")


# ── Save artifact ─────────────────────────────────────────────────────────────

def save_results(text: str) -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    path = os.path.join(os.path.dirname(__file__), f"scan_{ts}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"[SAVED] {path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("[Alpha Scanner] Starting scan...")

    gainers = fetch_top_gainers()
    print(f"[Alpha Scanner] Found {len(gainers)} top gainers")

    tickers = [s["ticker"] for s in gainers]
    print(f"[Alpha Scanner] Fetching news for: {', '.join(tickers)}")
    news_map = fetch_news(tickers)

    html = build_html(gainers, news_map)
    text = build_text(gainers, news_map)

    date_short = datetime.now().strftime("%b %-d")
    send_email(f"📈 Alpha Scanner — Top Movers {date_short}", html, text)

    save_results(text)
    print("[Alpha Scanner] Done.")


if __name__ == "__main__":
    main()
