"""
Optimal Strategy Scanner — Cloud Version
Scans universe for ACTIVE Multi-Timeframe MSS setups:
  - Daily trend confirmed (2 HH+HL or 2 LH+LL using 2-bar lookback)
  - 15m close just broke last 6-bar swing in trend direction (within last 2 bars)
Emails when setups are firing right now.
"""

import sys, os, datetime, smtplib, json
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import yfinance as yf
import pandas as pd
import numpy as np

try:
    import pytz
    ET = pytz.timezone("US/Eastern")
except ImportError:
    ET = None

# ── TIME WINDOW GUARD ────────────────────────────────────────────────────────
def in_scan_window():
    """Only run between 9:30 AM and 12:30 PM ET, Mon-Fri."""
    if ET is None:
        return True   # fail-open if pytz unavailable
    now_et = datetime.datetime.now(ET)
    if now_et.weekday() >= 5:      # Sat/Sun
        return False
    minutes_since_open = (now_et.hour * 60 + now_et.minute) - (9 * 60 + 30)
    return 0 <= minutes_since_open <= 180   # 9:30 AM to 12:30 PM ET (180 min window)

# ── CONFIG ───────────────────────────────────────────────────────────────────
SMTP_USER = os.environ.get("SMTP_USER", "2daysale@gmail.com")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
EMAIL_TO  = os.environ.get("EMAIL_TO",  "2daysale@gmail.com")

HTF_LOOKBACK = 2
LTF_LOOKBACK = 6
TREND_COUNT  = 2
RR_T1        = 1.5
RR_T2        = 3.0
SIGNAL_RECENCY_BARS = 2   # only alert if MSS broke in last N 15m bars

# Curated universe — stocks that backtested well or have clean structure
UNIVERSE = [
    # Tech / mega-cap that trend well
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","AMD","NFLX","AVGO",
    "ORCL","CRM","ADBE","INTC","QCOM","TXN","SMCI","CRWD","PANW","SNOW","DDOG","NET","MDB",
    # High-beta / momentum
    "COIN","MSTR","PLTR","SOFI","HOOD","RBLX","UBER","LYFT","SHOP","ABNB","DASH",
    "ROKU","SPOT","SNAP","PINS","CVNA","HOOD","AFRM","UPST","PYPL",
    # Financials
    "JPM","GS","MS","BAC","WFC","C","BLK","SCHW","COF","AXP",
    # Energy
    "XOM","CVX","COP","OXY","MPC","VLO","SLB","HAL","DVN",
    # Healthcare / biotech
    "JNJ","UNH","LLY","PFE","MRK","ABBV","TMO","ISRG","REGN","MRNA","BNTX",
    # Consumer
    "WMT","HD","COST","TGT","LOW","DIS","NKE","SBUX","MCD","CMG",
    # Crypto miners
    "MARA","RIOT","HUT","CLSK","BITF",
    # Industrials
    "CAT","BA","GE","HON","RTX","LMT","DE",
    # China / EV
    "BABA","JD","NIO","XPEV","RIVN","LCID","F","GM",
]
UNIVERSE = sorted(set(UNIVERSE))

# ── PIVOT DETECTION ──────────────────────────────────────────────────────────
def find_pivots(highs, lows, lookback):
    n  = len(highs)
    ph = pd.Series(np.nan, index=highs.index)
    pl = pd.Series(np.nan, index=lows.index)
    for i in range(lookback, n - lookback):
        h = highs.iloc[i]
        l = lows.iloc[i]
        if all(h > highs.iloc[i-j] for j in range(1, lookback+1)) and \
           all(h > highs.iloc[i+j] for j in range(1, lookback+1)):
            ph.iloc[i] = h
        if all(l < lows.iloc[i-j] for j in range(1, lookback+1)) and \
           all(l < lows.iloc[i+j] for j in range(1, lookback+1)):
            pl.iloc[i] = l
    return ph, pl

def detect_htf_trend(htf_df):
    """Returns latest trend bias: 1=up, -1=down, 0=none."""
    ph, pl = find_pivots(htf_df["High"], htf_df["Low"], HTF_LOOKBACK)
    highs = ph.dropna().iloc[-(TREND_COUNT+1):].tolist()[::-1]  # most recent first
    lows  = pl.dropna().iloc[-(TREND_COUNT+1):].tolist()[::-1]

    if len(highs) >= TREND_COUNT + 1 and len(lows) >= TREND_COUNT + 1:
        if all(highs[k] > highs[k+1] for k in range(TREND_COUNT)) and \
           all(lows[k]  > lows[k+1]  for k in range(TREND_COUNT)):
            return 1
        if all(highs[k] < highs[k+1] for k in range(TREND_COUNT)) and \
           all(lows[k]  < lows[k+1]  for k in range(TREND_COUNT)):
            return -1
    return 0

# ── SCAN ONE TICKER ──────────────────────────────────────────────────────────
def scan_ticker(ticker):
    try:
        tk  = yf.Ticker(ticker)
        htf = tk.history(period="60d", interval="1d").dropna()
        ltf = tk.history(period="5d",  interval="15m").dropna()
        if len(htf) < 20 or len(ltf) < 20:
            return None

        # 1) HTF trend
        trend = detect_htf_trend(htf)
        if trend == 0:
            return None

        # 2) Find LTF swings
        ph, pl = find_pivots(ltf["High"], ltf["Low"], LTF_LOOKBACK)

        # Most recent confirmed swings (must be confirmed = LTF_LOOKBACK bars before NOW)
        confirmed_idx = len(ltf) - LTF_LOOKBACK
        if confirmed_idx < LTF_LOOKBACK:
            return None

        # Find last swing high/low (going backwards from confirmed_idx)
        last_swing_high = None
        last_swing_high_idx = None
        last_swing_low  = None
        last_swing_low_idx  = None
        for i in range(confirmed_idx, LTF_LOOKBACK - 1, -1):
            if last_swing_high is None and not np.isnan(ph.iloc[i]):
                last_swing_high = float(ph.iloc[i])
                last_swing_high_idx = i
            if last_swing_low is None and not np.isnan(pl.iloc[i]):
                last_swing_low = float(pl.iloc[i])
                last_swing_low_idx = i
            if last_swing_high is not None and last_swing_low is not None:
                break

        if last_swing_high is None or last_swing_low is None:
            return None

        price = float(ltf["Close"].iloc[-1])

        # 3) Check for active MSS in trend direction within last N bars
        signal     = None
        bars_since = None
        for offset in range(SIGNAL_RECENCY_BARS):
            idx = len(ltf) - 1 - offset
            if idx < 0: break
            close_i = float(ltf["Close"].iloc[idx])
            # bullish MSS: trend up, close broke above swing high formed BEFORE this bar
            if trend == 1 and close_i > last_swing_high and idx > last_swing_high_idx + LTF_LOOKBACK:
                signal     = "LONG"
                bars_since = offset
                break
            if trend == -1 and close_i < last_swing_low and idx > last_swing_low_idx + LTF_LOOKBACK:
                signal     = "SHORT"
                bars_since = offset
                break

        if signal is None:
            return None

        # 4) Compute trade levels
        if signal == "LONG":
            entry = price
            stop  = last_swing_low
            risk  = entry - stop
            if risk <= 0: return None
            t1 = entry + risk * RR_T1
            t2 = entry + risk * RR_T2
        else:
            entry = price
            stop  = last_swing_high
            risk  = stop - entry
            if risk <= 0: return None
            t1 = entry - risk * RR_T1
            t2 = entry - risk * RR_T2

        # Daily change and volume context
        prev_close = float(htf["Close"].iloc[-2]) if len(htf) >= 2 else price
        chg_pct    = ((price - prev_close) / prev_close) * 100
        avg_vol    = float(htf["Volume"].mean())
        today_vol  = float(ltf["Volume"].sum())
        vol_ratio  = today_vol / avg_vol if avg_vol > 0 else 1

        return {
            "ticker": ticker,
            "direction": signal,
            "trend": "UP" if trend == 1 else "DOWN",
            "price": round(price, 2),
            "entry": round(entry, 2),
            "stop":  round(stop,  2),
            "t1":    round(t1,    2),
            "t2":    round(t2,    2),
            "risk_pct": round((risk/entry)*100, 2),
            "rr_t2": RR_T2,
            "chg_pct": round(chg_pct, 2),
            "vol_ratio": round(vol_ratio, 2),
            "bars_since": bars_since,
            "swing_high": round(last_swing_high, 2),
            "swing_low":  round(last_swing_low,  2),
        }
    except Exception:
        return None

# ── MAIN SCAN ────────────────────────────────────────────────────────────────
def run_scan():
    now = datetime.datetime.utcnow()
    print(f"Optimal Strategy scan: {now.strftime('%Y-%m-%d %H:%M UTC')} | {len(UNIVERSE)} tickers")

    setups = []
    for i, tk in enumerate(UNIVERSE):
        s = scan_ticker(tk)
        if s:
            setups.append(s)
        if (i+1) % 20 == 0:
            print(f"  {i+1}/{len(UNIVERSE)} scanned, {len(setups)} active setups")

    # Sort: most recent first, then by abs change
    setups.sort(key=lambda s: (s["bars_since"], -abs(s["chg_pct"])))

    print(f"\nFound {len(setups)} active MSS setups:")
    for s in setups:
        bars_str = "JUST FIRED" if s["bars_since"] == 0 else f"{s['bars_since']} bar(s) ago"
        print(f"  {s['ticker']:<6} {s['direction']:<5} ${s['price']:.2f} ({s['chg_pct']:+.1f}%) | {bars_str} | Risk {s['risk_pct']}% | T2 ${s['t2']}")

    # Save JSON
    with open("optimal_scan.json", "w") as f:
        json.dump({"scan_time": now.isoformat(), "setups": setups}, f, indent=2)

    if setups and SMTP_PASS:
        send_email(setups, now)
    else:
        print("No email sent (no setups or no SMTP_PASS)")

    return setups

# ── EMAIL ────────────────────────────────────────────────────────────────────
def send_email(setups, scan_time):
    longs  = [s for s in setups if s["direction"] == "LONG"]
    shorts = [s for s in setups if s["direction"] == "SHORT"]

    def rows(items, color):
        html = ""
        for s in items:
            recency = "JUST FIRED" if s["bars_since"] == 0 else f"{s['bars_since']*15}m ago"
            html += f"""<tr>
              <td style='padding:6px 10px;font-weight:bold;color:#fff;font-size:14px'>{s['ticker']}</td>
              <td style='padding:6px 10px;color:{color};font-weight:bold'>{s['direction']}</td>
              <td style='padding:6px 10px;color:#aaa'>{recency}</td>
              <td style='padding:6px 10px;color:#fff'>${s['price']:.2f}</td>
              <td style='padding:6px 10px;color:{"#00ff88" if s["chg_pct"]>0 else "#ff4444"}'>{s['chg_pct']:+.1f}%</td>
              <td style='padding:6px 10px;color:#00d4ff'>${s['entry']:.2f}</td>
              <td style='padding:6px 10px;color:#ff4444'>${s['stop']:.2f}</td>
              <td style='padding:6px 10px;color:#ffcc44'>${s['t1']:.2f}</td>
              <td style='padding:6px 10px;color:#00ff88;font-weight:bold'>${s['t2']:.2f}</td>
              <td style='padding:6px 10px;color:#aaa'>{s['risk_pct']}%</td>
              <td style='padding:6px 10px;color:#aaa'>{s['vol_ratio']:.1f}x</td>
            </tr>"""
        return html

    th = lambda h: f"<th style='padding:8px 10px;background:#1a1a2e;color:#00d4ff;text-align:left'>{h}</th>"
    headers = "".join(th(h) for h in ["Ticker","Dir","Fired","Price","Chg%","Entry","Stop","T1","T2","Risk%","Vol"])

    et = (scan_time - datetime.timedelta(hours=4)).strftime('%I:%M %p ET')

    html = f"""<html><body style='font-family:Arial;background:#0a0a0a;color:#eee;padding:20px;margin:0'>
<div style='max-width:1100px;margin:0 auto'>
<h2 style='color:#00d4ff;margin-bottom:5px'>🎯 Optimal Strategy Setups — {et}</h2>
<p style='color:#888;margin:0 0 20px'>
  Multi-TF Market Structure Shift &nbsp;|&nbsp; Daily trend + 15m MSS entry<br>
  <span style='color:#0f0'>{len(longs)} LONG</span> &nbsp;&nbsp; <span style='color:#f55'>{len(shorts)} SHORT</span>
</p>

{f"<h3 style='color:#00ff88'>🟢 LONG SETUPS ({len(longs)})</h3><table style='border-collapse:collapse;width:100%;background:#111;border-radius:6px'><tr>{headers}</tr>{rows(longs,'#00ff88')}</table>" if longs else ""}

{f"<h3 style='color:#ff4444;margin-top:30px'>🔴 SHORT SETUPS ({len(shorts)})</h3><table style='border-collapse:collapse;width:100%;background:#111;border-radius:6px'><tr>{headers}</tr>{rows(shorts,'#ff4444')}</table>" if shorts else ""}

<div style='margin-top:30px;padding:15px;background:#111;border-left:3px solid #00d4ff;border-radius:4px'>
  <p style='color:#00d4ff;margin:0 0 8px;font-weight:bold'>Strategy Recap:</p>
  <p style='color:#aaa;font-size:12px;margin:0'>
    1. Daily must show 2 HH+HL (long) or 2 LH+LL (short) — 2-bar swing lookback<br>
    2. 15m identifies swings using 6-bar lookback<br>
    3. Entry = 15m close breaks last opposing swing in trend direction<br>
    4. Stop = beyond opposing swing | T1 = 1.5R (take half) | T2 = 3R (runner)<br>
    5. Risk 1% account per trade
  </p>
</div>

<p style='color:#444;margin-top:30px;font-size:11px'>
  Backtest (60 days, 43 tickers): 35.7% WR, +0.06R expectancy, +17.1% return @ 1% risk<br>
  Not financial advice. Always verify before trading.
</p>
</div></body></html>"""

    msg = MIMEMultipart("alternative")
    top = setups[0]
    msg["Subject"] = f"🎯 OPT-MSS {et} — {len(longs)}L/{len(shorts)}S | Top: {top['ticker']} {top['direction']}"
    msg["From"]    = SMTP_USER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())
    print(f"Email sent to {EMAIL_TO}")

if __name__ == "__main__":
    if not in_scan_window():
        now_et = datetime.datetime.now(ET).strftime("%Y-%m-%d %H:%M %Z") if ET else "unknown"
        print(f"Outside scan window (9:30 AM – 12:30 PM ET, Mon-Fri). Now: {now_et}. Exiting.")
        sys.exit(0)
    run_scan()
