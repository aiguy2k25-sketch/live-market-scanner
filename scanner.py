"""
Live Market Scanner — Cloud Version (GitHub Actions)
Runs as a single scan, called on schedule by GitHub Actions workflow
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

# ── CONFIG (set via GitHub Secrets) ──────────────────────────────────────────
SMTP_USER  = os.environ.get("SMTP_USER", "2daysale@gmail.com")
SMTP_PASS  = os.environ.get("SMTP_PASS", "")
EMAIL_TO   = os.environ.get("EMAIL_TO",  "2daysale@gmail.com")
MIN_SCORE  = int(os.environ.get("MIN_SCORE", "45"))   # only email if top score >= this
TOP_N      = 25
MIN_PRICE  = 2.0
MIN_VOLUME = 500000

# ── UNIVERSE ─────────────────────────────────────────────────────────────────
UNIVERSE = [
    # Mega cap
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","AVGO","JPM","V",
    "XOM","UNH","LLY","MA","JNJ","PG","HD","MRK","ABBV","CVX",
    "KO","PEP","COST","WMT","BAC","NFLX","CRM","AMD","ORCL","INTC",
    "QCOM","TXN","HON","GE","CAT","BA","GS","MS","BLK","SCHW",
    "AMGN","GILD","REGN","VRTX","ISRG","TMO","ABT","MDT","SYK","BSX",
    # High beta / momentum
    "MSTR","COIN","RBLX","HOOD","PLTR","SOFI","UPST","AFRM","RIVN","LCID",
    "SMCI","CRWD","PANW","ZS","DDOG","NET","SNOW","MDB","CFLT","U",
    "SHOP","SQ","PYPL","UBER","LYFT","ABNB","DASH","PINS","SNAP","TWLO",
    "ROKU","SPOT","CVNA","GME","AMC","BB","SNDL",
    # Financials
    "WFC","C","USB","PNC","TFC","COF","AXP","DFS","SYF","ALLY",
    # Energy
    "OXY","MPC","VLO","PSX","HAL","SLB","BKR","DVN","PXD","FANG",
    # Biotech
    "MRNA","BNTX","PFE","BIIB","ALNY","INCY","EXEL","FATE","EDIT",
    # Inverse / leveraged ETFs
    "SQQQ","SPXS","SDOW","UVXY","VXX","SOXS","TZA","FAZ","ERY","DUST",
    "TQQQ","SPXL","UPRO","UDOW","SOXL","TNA","FAS","ERX","NUGT","JNUG",
    "QQQ","SPY","IWM","DIA","SMH","XLF","XLE","XLK","XLV","XLI",
    # China / emerging
    "BABA","JD","PDD","BIDU","NIO","XPEV","LI","KWEB","FXI",
    # Crypto-adjacent
    "MARA","RIOT","HUT","BITF","CLSK","IBIT","FBTC","GBTC",
    # StockInvest.us Top 20 Buy List (added 2026-05)
    "TRDA","FATE","PERI","ASIX","ROST","PUBM","SBR","SSSS","SRPT","VNOM",
    "AAOI","FLYW","ADV","KOS","KNF","SEZL","PRAA","AUDC","JAKK",
]
UNIVERSE = sorted(set(UNIVERSE))

# ── INDICATORS ────────────────────────────────────────────────────────────────
def rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def vwap(df):
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    return (tp * df["Volume"]).cumsum() / df["Volume"].cumsum()

def atr(df, period=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()

# ── SETUP DETECTION ───────────────────────────────────────────────────────────
def detect_setups(ticker):
    try:
        tk   = yf.Ticker(ticker)
        df1d = tk.history(period="5d", interval="1d")
        df5m = tk.history(period="1d", interval="5m")

        if df1d.empty or df5m.empty or len(df5m) < 20:
            return None

        price   = float(df5m["Close"].iloc[-1])
        vol_now = int(df5m["Volume"].sum())
        avg_vol = int(df1d["Volume"].mean()) if len(df1d) > 1 else 0

        if price < MIN_PRICE or avg_vol < MIN_VOLUME:
            return None

        close5m   = df5m["Close"]
        rsi_val   = float(rsi(close5m).iloc[-1])
        vwap_val  = float(vwap(df5m).iloc[-1])
        ema9      = float(close5m.ewm(span=9).mean().iloc[-1])
        ema20     = float(close5m.ewm(span=20).mean().iloc[-1])
        ema50     = float(close5m.ewm(span=50).mean().iloc[-1]) if len(close5m) >= 50 else ema20
        atr_val   = float(atr(df5m).iloc[-1])
        atr_pct   = (atr_val / price) * 100
        prev_close = float(df1d["Close"].iloc[-2]) if len(df1d) >= 2 else price
        chg_pct   = ((price - prev_close) / prev_close) * 100
        vol_ratio = vol_now / avg_vol if avg_vol > 0 else 1.0
        day_high  = float(df5m["High"].max())
        day_low   = float(df5m["Low"].min())

        score     = 0
        signals   = []
        direction = "NEUTRAL"

        if rsi_val <= 30:
            score += 25; signals.append(f"RSI oversold ({rsi_val:.0f})"); direction = "LONG"
        elif rsi_val <= 40:
            score += 12; signals.append(f"RSI low ({rsi_val:.0f})"); direction = "LONG"
        elif rsi_val >= 70:
            score += 25; signals.append(f"RSI overbought ({rsi_val:.0f})"); direction = "SHORT"
        elif rsi_val >= 60:
            score += 12; signals.append(f"RSI high ({rsi_val:.0f})"); direction = "SHORT"

        vwap_diff = ((price - vwap_val) / vwap_val) * 100
        if price > vwap_val * 1.002:
            score += 15; signals.append(f"Above VWAP +{vwap_diff:.1f}%")
            if direction == "NEUTRAL": direction = "LONG"
        elif price < vwap_val * 0.998:
            score += 15; signals.append(f"Below VWAP {vwap_diff:.1f}%")
            if direction == "NEUTRAL": direction = "SHORT"
        else:
            score += 8; signals.append("At VWAP")

        prev_ema9  = float(close5m.ewm(span=9).mean().iloc[-2])
        prev_ema20 = float(close5m.ewm(span=20).mean().iloc[-2])
        if prev_ema9 <= prev_ema20 and ema9 > ema20:
            score += 20; signals.append("EMA 9 crossed above 20"); direction = "LONG"
        elif prev_ema9 >= prev_ema20 and ema9 < ema20:
            score += 20; signals.append("EMA 9 crossed below 20"); direction = "SHORT"
        elif ema9 > ema20 > ema50:
            score += 10; signals.append("EMA stack bullish")
            if direction == "NEUTRAL": direction = "LONG"
        elif ema9 < ema20 < ema50:
            score += 10; signals.append("EMA stack bearish")
            if direction == "NEUTRAL": direction = "SHORT"

        if vol_ratio >= 3.0:
            score += 20; signals.append(f"Volume spike {vol_ratio:.1f}x")
        elif vol_ratio >= 2.0:
            score += 12; signals.append(f"High volume {vol_ratio:.1f}x")
        elif vol_ratio >= 1.5:
            score += 6;  signals.append(f"Above avg vol {vol_ratio:.1f}x")

        near_high = ((day_high - price) / day_high) * 100
        near_low  = ((price - day_low)  / day_low)  * 100
        if near_high < 0.3 and chg_pct > 0:
            score += 15; signals.append("Near day high — breakout watch"); direction = "LONG"
        elif near_low < 0.3 and chg_pct < 0:
            score += 15; signals.append("Near day low — breakdown watch"); direction = "SHORT"

        if abs(chg_pct) >= 5:
            score += 15; signals.append(f"Big move {chg_pct:+.1f}%")
        elif abs(chg_pct) >= 2:
            score += 8;  signals.append(f"Move {chg_pct:+.1f}%")

        if atr_pct >= 2.0:
            score += 10; signals.append(f"High ATR {atr_pct:.1f}%")
        elif atr_pct >= 1.0:
            score += 5;  signals.append(f"ATR {atr_pct:.1f}%")
        else:
            score -= 5

        if not signals or direction == "NEUTRAL":
            return None

        if direction == "LONG":
            entry  = round(price, 2)
            stop   = round(price - atr_val, 2)
            target = round(price + atr_val * 2, 2)
        else:
            entry  = round(price, 2)
            stop   = round(price + atr_val, 2)
            target = round(price - atr_val * 2, 2)

        return {
            "ticker": ticker, "score": score, "direction": direction,
            "price": price, "chg_pct": round(chg_pct, 2),
            "rsi": round(rsi_val, 1), "vwap": round(vwap_val, 2),
            "atr_pct": round(atr_pct, 2), "vol_ratio": round(vol_ratio, 2),
            "entry": entry, "stop": stop, "target": target,
            "signals": signals,
        }
    except Exception:
        return None

# ── SCAN ──────────────────────────────────────────────────────────────────────
def run_scan():
    now = datetime.datetime.utcnow()
    print(f"Scan started: {now.strftime('%Y-%m-%d %H:%M UTC')} | {len(UNIVERSE)} tickers")

    results = []
    for i, ticker in enumerate(UNIVERSE):
        r = detect_setups(ticker)
        if r and r["score"] >= 20:
            results.append(r)
        if (i+1) % 30 == 0:
            print(f"  {i+1}/{len(UNIVERSE)} scanned — {len(results)} setups so far")

    results.sort(key=lambda x: x["score"], reverse=True)
    top = results[:TOP_N]

    long_r  = [r for r in top if r["direction"] == "LONG"]
    short_r = [r for r in top if r["direction"] == "SHORT"]

    print(f"\nTotal setups: {len(results)} | Long: {len(long_r)} | Short: {len(short_r)}")
    for r in top[:10]:
        print(f"  {r['ticker']:<6} {r['direction']:<5} Score:{r['score']} ${r['price']:.2f} {r['chg_pct']:+.1f}% | {' | '.join(r['signals'][:2])}")

    # Save JSON artifact
    with open("last_scan.json", "w") as f:
        json.dump({"scan_time": now.isoformat(), "results": top}, f, indent=2)

    # Email only if top score is good enough
    top_score = top[0]["score"] if top else 0
    if top_score >= MIN_SCORE and SMTP_PASS:
        send_email(top, long_r, short_r, now)
    else:
        print(f"No email sent (top score={top_score}, min={MIN_SCORE})")

    return top

# ── EMAIL ─────────────────────────────────────────────────────────────────────
def send_email(results, long_r, short_r, scan_time):
    def rows(items, color):
        html = ""
        for r in items:
            sigs = " | ".join(r["signals"][:3])
            html += f"""<tr>
              <td style='padding:5px 8px;font-weight:bold;color:#fff'>{r['ticker']}</td>
              <td style='padding:5px 8px;color:{color};font-weight:bold'>{r['direction']}</td>
              <td style='padding:5px 8px;color:#fff'>{r['score']}</td>
              <td style='padding:5px 8px;color:#fff'>${r['price']:.2f}</td>
              <td style='padding:5px 8px;color:{"#00ff88" if r["chg_pct"]>0 else "#ff4444"}'>{r['chg_pct']:+.1f}%</td>
              <td style='padding:5px 8px;color:#fff'>{r['rsi']:.0f}</td>
              <td style='padding:5px 8px;color:#fff'>{r['vol_ratio']:.1f}x</td>
              <td style='padding:5px 8px;color:#fff'>${r['entry']:.2f}</td>
              <td style='padding:5px 8px;color:#ff4444'>${r['stop']:.2f}</td>
              <td style='padding:5px 8px;color:#00ff88'>${r['target']:.2f}</td>
              <td style='padding:5px 8px;color:#aaa;font-size:11px'>{sigs}</td>
            </tr>"""
        return html

    th = lambda h: f"<th style='padding:7px 8px;background:#1a1a2e;color:#00d4ff;text-align:left'>{h}</th>"
    headers = "".join(th(h) for h in ["Ticker","Dir","Score","Price","Chg%","RSI","Vol","Entry","Stop","Target","Signals"])

    et_time = scan_time.strftime('%I:%M %p ET')

    html = f"""<html><body style='font-family:Arial,sans-serif;background:#0a0a0a;color:#eee;padding:20px;margin:0'>
    <div style='max-width:1100px;margin:0 auto'>
    <h2 style='color:#00d4ff;margin-bottom:5px'>Live Market Scan — {et_time}</h2>
    <p style='color:#888;margin-top:0'>{len(results)} setups found &nbsp;|&nbsp; {len(long_r)} long &nbsp;|&nbsp; {len(short_r)} short</p>

    <h3 style='color:#00ff88'>LONG SETUPS ({len(long_r)})</h3>
    <table style='border-collapse:collapse;width:100%;background:#111;border-radius:6px'>
      <tr>{headers}</tr>{rows(long_r,'#00ff88')}
    </table>

    <h3 style='color:#ff4444;margin-top:30px'>SHORT SETUPS ({len(short_r)})</h3>
    <table style='border-collapse:collapse;width:100%;background:#111;border-radius:6px'>
      <tr>{headers}</tr>{rows(short_r,'#ff4444')}
    </table>

    <p style='color:#444;margin-top:30px;font-size:11px'>
      Not financial advice. For educational/research purposes only.<br>
      Entry/Stop/Target are ATR-based suggestions — always verify before trading.
    </p>
    </div></body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Scan {et_time} — {len(long_r)}L / {len(short_r)}S | Top: {results[0]['ticker']} ({results[0]['score']})"
    msg["From"]    = SMTP_USER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())
    print(f"Email sent to {EMAIL_TO}")

# ── RUN ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not in_scan_window():
        now_et = datetime.datetime.now(ET).strftime("%Y-%m-%d %H:%M %Z") if ET else "unknown"
        print(f"Outside scan window (9:30 AM – 12:30 PM ET, Mon-Fri). Now: {now_et}. Exiting.")
        sys.exit(0)
    run_scan()
