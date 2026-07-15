"""
Pre-Market Catalyst Scanner
Scrapes overnight news from multiple sources and ranks stocks by move potential.
Sources: Yahoo Finance, Benzinga RSS, Finviz News, MarketWatch, Seeking Alpha RSS,
         Investing.com (via RSS), MarketChameleon, StockAnalysis.
"""

import asyncio
import os
import re
import smtplib
import socket
import sys
import time

# Safety net: hard cap on ALL network operations (feedparser, smtplib, etc.)
# Prevents a single hung feed from stalling the whole run until the GitHub
# Actions job timeout kills it.
socket.setdefaulttimeout(15)
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")


def now_ct() -> datetime:
    """Actual Central time. GitHub runners are UTC; naive datetime.now() was
    being formatted with a hard-coded 'CT' suffix, so every report lied about
    its own timestamp (ran 12:00 UTC, printed '12:00 PM CT')."""
    return datetime.now(timezone.utc).astimezone(CT)
from collections import defaultdict
import feedparser
import requests
from bs4 import BeautifulSoup
from tabulate import tabulate
from colorama import init, Fore, Style

try:
    import portfolio as _portfolio
except Exception as _e:
    _portfolio = None
    print(f"[PORTFOLIO] module unavailable: {_e}")

init(autoreset=True)

# --- CONFIG ------------------------------------------------------------------

LOOKBACK_HOURS = 18          # how far back to consider "overnight"
TOP_N = 30                   # how many stocks to display
REQUEST_TIMEOUT = 12

# --- EMAIL CONFIG ------------------------------------------------------------
# Set these via environment variables (recommended) or hard-code for local use.
# For Gmail: use an App Password (myaccount.google.com/apppasswords)
#   SMTP_USER = your Gmail address
#   SMTP_PASS = 16-character App Password (no spaces)

EMAIL_TO      = os.environ.get("EMAIL_TO",   "2daysale@gmail.com")
SMTP_HOST     = os.environ.get("SMTP_HOST",  "smtp.gmail.com")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER",  "2daysale@gmail.com")
# SECURITY: never hard-code the app password here. The old one was committed
# to a public repo and must be revoked at myaccount.google.com/apppasswords.
# Set SMTP_PASS only via GitHub Actions Secrets (Settings > Secrets > Actions).
SMTP_PASS     = os.environ.get("SMTP_PASS", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# High-impact keywords -> score boost
CATALYST_WEIGHTS = {
    # Earnings
    "earnings beat": 12, "eps beat": 12, "revenue beat": 10, "raised guidance": 11,
    "beat estimates": 10, "beat expectations": 10, "record revenue": 9,
    "record earnings": 9, "earnings miss": -8, "eps miss": -8, "missed estimates": -8,
    "lowered guidance": -9, "cut guidance": -9, "withdrew guidance": -10,
    # M&A
    "acquisition": 11, "merger": 11, "takeover": 12, "buyout": 12,
    "deal": 7, "agreement": 6, "acquires": 11, "acquired": 11,
    # Fda / biotech
    "fda approval": 15, "fda approved": 15, "fda clearance": 14,
    "breakthrough therapy": 13, "phase 3": 10, "phase iii": 10, "clinical trial": 8,
    "fda rejection": -14, "complete response letter": -12, "clinical hold": -11,
    # Short squeeze / unusual
    "short squeeze": 10, "heavily shorted": 8,
    # Analyst
    "upgrade": 8, "downgrade": -7, "price target raised": 7, "price target cut": -6,
    "initiated": 5, "outperform": 5, "buy rating": 5, "sell rating": -5,
    # Contract / partnership
    "contract": 8, "partnership": 7, "collaboration": 6, "licensing": 6,
    "government contract": 9, "awarded": 7,
    # Restructuring / risk
    "bankruptcy": -13, "chapter 11": -13, "delisted": -12, "fraud": -11,
    "sec investigation": -11, "class action": -9, "subpoena": -8,
    "going concern": -10, "layoffs": -4, "restructuring": 3,
    # General positive
    "surged": 5, "soared": 5, "jumped": 5, "rallied": 4, "gains": 3,
    "record high": 6, "all-time high": 6, "breakout": 5,
    # General negative
    "plunged": -5, "tumbled": -5, "dropped": -4, "fell": -3, "declined": -3,
    "losses": -3, "warning": -5, "disappointing": -5,
    # 2026-07-14: ASML scored 0.0 Neutral on "Trillion Dollar Chip Rout"
    "rout": -6, "crash": -6, "craters": -6, "cratered": -6, "slump": -4,
    "selloff": -5, "sell-off": -5, "pummeled": -6, "sinks": -4, "sank": -4,
    "collapse": -7, "tanks": -4, "tanked": -4,
}

# --- HELPERS -----------------------------------------------------------------

import ticker_universe as TU

# Tier-1 (high confidence) patterns: explicit ticker syntax in financial text.
# Matches: $AVGO, (NASDAQ: AVGO), (NYSE: BRK.A), NYSE American: AIB, (AMEX: XYZ)
STRONG_TICKER_RE = re.compile(
    r'(?:\$([A-Z]{1,5})\b'
    r'|\((?:NASDAQ|NYSE(?:\s+American)?|AMEX|OTC|CBOE)\s*:\s*([A-Z]{1,5}(?:\.[A-Z])?)\)'
    r'|\b(?:NASDAQ|NYSE(?:\s+American)?|AMEX)\s*:\s*([A-Z]{1,5}(?:\.[A-Z])?)\b)'
)

# Tier-2 (bare word) pattern — only accepted after universe validation.
TICKER_RE = re.compile(r'\b([A-Z]{2,5})\b')

# Tickers that are also common English words / proper nouns. A bare-word match
# on these is NOT enough — they require Tier-1 syntax ($BULL, "(NASDAQ: COCO)")
# or their company name appearing in the same text.
AMBIGUOUS_TICKERS = {
    "ALL", "AN", "ARE", "BIG", "BULL", "CAKE", "CARS", "COCO", "COOL", "DASH",
    "EAT", "EVER", "FAST", "FIVE", "FIZZ", "FLY", "FORM", "FUN", "GAIN", "GOOD",
    "GROW", "HAS", "HEAR", "HOG", "HOPE", "HUGE", "ICE", "JACK", "JOB", "KEY",
    "LAND", "LIFE", "LIKE", "LOVE", "MAIN", "MAX", "MIND", "NET", "NEXT", "NICE",
    "NOW", "ONE", "OPEN", "PEAK", "PLAY", "PLUS", "PRO", "RACE", "REAL", "RIDE",
    "ROAD", "ROCK", "ROOT", "SAFE", "SEE", "SHOP", "SITE", "SKIN", "SNOW", "SO",
    "STEP", "TASK", "TEAM", "TECH", "TELL", "TILE", "TREE", "TRIP", "TRUE",
    "TURN", "TWO", "VG", "WAY", "WELL", "WIRE", "WOW", "YETI", "YOU", "NP",
    "SM", "CC", "GO", "IT", "ME", "MO", "ON", "OUT", "RUN", "SAVE", "TV",
    # "UPS" in PR text is almost always Uninterruptible Power Supply, not
    # United Parcel Service (2026-07-14: a CBAK Energy battery-testing release
    # was attributed to UPS). Require "(NYSE: UPS)" / $UPS / "United Parcel".
    "UPS",
}

# Sell-side firms that AUTHOR analyst actions about OTHER companies. When one
# of these appears in an upgrade/downgrade/forecast headline, the bank is the
# analyst, not the subject -- 2026-07-14: "HSBC loses conviction in leading
# drugmaker" scored HSBC bearish. The downgraded drugmaker owns that score.
SELL_SIDE_BANKS = {
    "GS", "MS", "JPM", "BAC", "C", "WFC", "UBS", "HSBC", "DB", "BCS",
    "RBC", "BMO", "TD", "JEF", "CS", "LAZ", "PJT", "EVR", "RJF", "SF", "MUFG",
}
ANALYST_ACTION_RE = re.compile(
    r'\b(upgrad\w+|downgrad\w+|initiat\w+\s+coverage|price\s+target|'
    r'overweight|underweight|outperform|underperform|buy\s+rating|'
    r'sell\s+rating|neutral\s+rating|maintains|reiterates|top\s+pick|'
    r'loses\s+conviction|forecasts?|sees\s+\$|names?\s+.{0,30}\s+pick)\b',
    re.IGNORECASE)

# Common words to exclude from ticker detection
EXCLUDE_WORDS = {
    "A","B","C","D","E","F","G","H","I","J","K","L","M","N","O","P","Q","R","S","T","U","V","W","X","Y","Z",
    "AM","PM","ET","PT","CT","MT","AT","TO","OF","IN","ON","BY","AS","IS","BE","DO","SO","GO","IF","IT","MY",
    "OR","UP","US","OK","NO","PR","IP","AI","ML","EV","AR","VR","PE","VC","CEO","CFO","COO","CTO","IPO","SPX",
    "DOW","GDP","CPI","PPI","FED","SEC","FDA","IRS","ETF","ADR","OTC","EPS","BPS","SPAC","ESG","EUR","GBP",
    "JPY","USD","CAD","AUD","CHF","CNY","NOW","NEW","GET","SET","THE","FOR","AND","BUT","NOT","ALL","ANY",
    "HAS","HAD","WAS","DID","CAN","MAY","EST","INC","LLC","LTD","PLC","CORP","NYSE","NASDAQ","NYSE","WTI",
    "BRENT","ATH","ATL","RTH","AFTER","HOURS","PRE","OPEN","CLOSE","HIGH","LOW","WATCH","NEWS","RATE",
    "RATES","RISK","BOND","GOLD","CASH","BANK","LOAN","DEBT","FUND","STOCK","SHARES","TRADE","CALL","PUT",
    "BUY","SELL","HOLD","LONG","SHORT","RALLY","WEEK","YEAR","HALF","FULL","FIRST","SECOND","THIRD","FOURTH",
    "Q1","Q2","Q3","Q4","FY","YOY","QOQ","MOM","DAY","DAYS","MONTH","MONTHS","YEAR","YEARS",
    "TODAY","DAILY","WEEKLY","MONTHLY","ANNUAL","QUARTER","QUARTERLY","CONSENSUS","FORECAST","OUTLOOK",
    "REPORT","RESULTS","UPDATE","SUMMARY","ANALYSIS","MARKET","ECONOMY","ECONOMIC","SECTOR","INDUSTRY",
    "SHARE","PRICE","GAIN","LOSS","RISE","FALL","JUMP","DROP","MOVE","PUSH","PULL","RUN","HOLD",
    "STRONG","WEAK","MIXED","FLAT","RANGE","INDEX","INDICES","VOLUME","AVERAGE","MEAN","MEDIAN",
    "AFTER","BEFORE","DURING","WHILE","ABOUT","ABOVE","BELOW","OVER","UNDER","NEAR","NEXT","LAST",
    "FROM","INTO","WITH","WITHOUT","AMONG","BETWEEN","THROUGH","ACROSS","ALONG","AROUND",
    "AGAIN","ALSO","BOTH","EACH","EVEN","EVER","JUST","MANY","MORE","MOST","MUCH","MUST","NEED","ONLY",
    "OTHER","SOME","SUCH","THAN","THEN","THEY","THIS","THUS","UPON","VERY","WELL","WERE","WHAT","WHEN",
    "WHOM","WHY","WILL","WITH","YOUR",
}

# HARD collisions: these ARE real listed tickers (LNG=Cheniere, AG=First
# Majestic, EU=enCore Energy, UBS=UBS Group, UK=Ucommune), which is exactly why
# universe validation waved them through. They are also countries, currencies,
# commodities, macro indicators and corporate suffixes. A bare-word match is
# never enough: require Tier-1 syntax ($LNG) or the company name in the text.
HARD_COLLISIONS = {
    # countries / regions / geopolitics
    "EU", "UK", "US", "UN", "AU", "CA", "CN", "DE", "FR", "JP", "IN", "BR", "MX", "RU",
    # commodities / energy
    "LNG", "AG", "AU", "OIL", "GAS", "WTI", "CL", "NG", "HG", "PT", "PD",
    # macro indicators / institutions
    "PMI", "CPI", "PPI", "GDP", "FED", "ECB", "BOE", "BOJ", "IMF", "OPEC", "NFP", "ISM",
    # org / government acronyms that ARE also real listed tickers.
    # 2026-07-14 scan: "WTO" (World Trade Organization in a fertiliser-trade
    # story) and "NHS" (UK National Health Service in a PPE story; NHS is the
    # Neuberger Berman High Yield fund) both ranked as catalyst stocks.
    "WTO", "NHS", "WHO", "EPA", "DOJ", "FTC", "FCC", "DOE", "DOT", "FBI",
    "NSA", "UAW", "OSHA", "CDC", "NIH", "HHS", "FAA", "TSA", "CMS", "PPE",
    # corporate suffixes (Aebi Schmidt Group *AG*, Volkswagen *AG*, UAB = the
    # Lithuanian LLC prefix that put AEI on the 2026-07-14 scan)
    "AG", "SA", "NV", "SE", "PLC", "GMBH", "KK", "AB", "AS", "OY", "UAB",
    # banks/brokers that author reports about other companies
    "UBS", "RBC", "BMO", "TD", "MS", "GS", "BCS", "DB", "CS", "JEF", "STT",
    # exchanges / indices
    "SPX", "NDX", "DJI", "RUT", "VIX", "FTSE", "DAX", "CAC", "TSX", "ASX",
}

# Any 2-letter bare token is almost always a word, not a ticker. Require
# corroboration for all of them.
def _needs_corroboration(t: str) -> bool:
    return t in HARD_COLLISIONS or t in AMBIGUOUS_TICKERS or len(t) == 2


# Known valid tickers to boost confidence
KNOWN_TICKERS = {
    "AAPL","MSFT","GOOGL","AMZN","META","TSLA","NVDA","AMD","INTC","QCOM","AVGO","ARM",
    "JPM","BAC","WFC","GS","MS","C","USB","PNC","TFC","KEY","SCHW","BLK","HOOD","SOFI",
    "XOM","CVX","COP","OXY","SLB","HAL","DVN","MPC","VLO","PSX","WMB","ET","KMI",
    "JNJ","PFE","ABBV","MRK","BMY","LLY","AMGN","GILD","BIIB","REGN","VRTX","MRNA","BNTX",
    "HD","WMT","COST","TGT","LOW","AMZN","DG","DLTR","KR","CVS","WBA","RAD",
    "BA","LMT","RTX","NOC","GD","HII","L3H","LDOS","SAIC","KTOS",
    "T","VZ","TMUS","DISH","ATUS","LUMN","FYBR","CMCSA","CHTR",
    "NFLX","DIS","WBD","PARA","FOXA","NYT","SPOT","SNAP","PINS","RBLX","U",
    "CRM","NOW","ORCL","SAP","INTU","ADBE","WDAY","ZM","DOCU","OKTA","DDOG","MDB",
    "PYPL","SQ","V","MA","AXP","COF","DFS","SYF","AFRM","UPST",
    "SPY","QQQ","IWM","DIA","GLD","SLV","TLT","HYG","LQD","VIX",
    "GME","AMC","BBBY","CLOV","MVIS","WISH","EXPR","KOSS","BB","NOK",
    "COIN","MSTR","MARA","RIOT","HUT","CLSK","BTBT","IREN",
    "NIO","LI","XPEV","RIVN","LCID","F","GM","STLA","TM","HMC","SONY",
    "PLTR","RKLB","ASTR","SPCE","RDW","MNTS",
    "SMCI","CRWD","PANW","FTNT","ZS","S","CYBR","TENB","QLYS",
}


# --- LAW-FIRM PR SPAM --------------------------------------------------------
# PR wires (GlobeNewswire, PRNewswire, ACCESSWIRE, Business Wire) are flooded
# with plaintiff securities-firm advertising. These are solicitations, not
# catalysts, and they arrive in BULK: one company can generate 15+ near-identical
# "investor alert" releases from 15 different firms in a single night.
#
# Two distinct ways they corrupt the scan:
#
#   1. VOLUME. Every one of these carries explicit "(NASDAQ: XYZ)" syntax, which
#      is Tier-1 in extract_tickers() -- accepted unconditionally, full confidence.
#      The dedupe pass above keys on the headline, and each firm writes its own,
#      so 15 releases survive as 15 separate "mentions" across multiple "sources".
#      The ticker climbs the rank on pure ad volume.
#
#   2. POLARITY. Merger-objection firms (Monteverde, Halper Sadeh, WeissLaw,
#      Rigrodsky) publish "investigating the merger of..." releases. Those strings
#      hit _MA_TRIGGERS below, and if _ACQUIRER_RE reads the company as the target,
#      the item collects the +15.0 M&A bonus. A lawsuit advertisement becomes a
#      top-ranked bullish buyout signal. This is the compounding failure.
#
# Filter runs at ingest, before scoring, so neither path can fire.

LAW_FIRMS = (
    # NOTE: match on distinctive template phrasing, not just firm names. Rosen's
    # wire template leads with "ROSEN, A LEADING LAW FIRM, Encourages..." -- the
    # string "rosen law" never appears in it.
    "rosen law", "a leading law firm", "leading investor rights law firm",
    "pomerantz", "levi & korsinsky", "levi and korsinsky",
    "glancy prongay", "bronstein, gewirtz", "bronstein gewirtz",
    "robbins geller", "robbins llp", "kessler topaz", "bragar eagel",
    "kahn swick", "schall law", "faruqi & faruqi", "faruqi and faruqi",
    "johnson fistel", "hagens berman", "gross law firm", "howard g. smith",
    "berger montague", "block & leviton", "block and leviton", "kirby mcinerney",
    "monteverde & associates", "monteverde and associates", "halper sadeh",
    "rigrodsky", "weisslaw", "ademi llp", "kaskela", "purcell & lefkowitz",
    "grabar law", "federman & sherwood", "wolf haldenstein", "saxena white",
    "labaton", "bernstein liebhard", "gainey mckenna", "scott+scott",
    "holzer & holzer", "andrews & springer", "brodsky & smith", "kuehn law",
    "thornton law", "cohen milstein", "abraham fruchter", "shareholder rights law",
    "law offices of",
)

LAW_FIRM_BOILERPLATE = (
    "investor alert", "shareholder alert", "class action lawsuit",
    "securities class action", "securities fraud class action",
    "lead plaintiff deadline", "lead plaintiff", "deadline reminder",
    "encourages investors", "encourages shareholders", "reminds investors",
    "reminds shareholders", "notifies investors", "investigating potential claims",
    "investigation on behalf of", "on behalf of shareholders",
    "on behalf of investors", "if you purchased", "if you suffered a loss",
    "recover your losses", "attorney advertising", "no cost to you",
    "contact the firm", "contingency fee", "prior results",
    "breach of fiduciary duty", "securities litigation", "notice of pendency",
    "class period", "may have been misled", "secure counsel",
    "before important deadline", "notifies shareholders", "announces the filing",
)


def is_law_firm_spam(text: str) -> bool:
    """True if the item is plaintiff-firm advertising rather than reporting.

    Trips on EITHER condition:
      * a known securities-litigation firm is named, OR
      * two or more solicitation boilerplate phrases appear.

    The 2-phrase floor is deliberate -- it keeps real reporting alive. A single
    mention of "class action" is not enough to kill an item, because
    "Judge certifies class action against XYZ" is genuine news and should still
    score its -9. But "Rosen Law Firm reminds investors of the lead plaintiff
    deadline in the XYZ securities class action" trips the firm name AND three
    boilerplate phrases.
    """
    low = text.lower()
    if any(f in low for f in LAW_FIRMS):
        return True
    return sum(1 for p in LAW_FIRM_BOILERPLATE if p in low) >= 2


# M&A is the one catalyst where the SAME headline is bullish for one ticker and
# bearish for another. "Vertex bets $10B on Crinetics" -> CRNX +100%, VRTX -2%.
# The old flat "acquisition: +11" scored the ACQUIRER as a top bull.
_MA_TRIGGERS = ("acquisition", "acquires", "acquire", "merger", "takeover",
                "buyout", "to buy", "agrees to acquire", "deal to buy")

_ACQUIRER_RE = re.compile(
    r'\b([A-Z][\w.&-]*(?:\s+[A-Z][\w.&-]*){0,3})\s+'
    r'(?:to\s+acquire|acquires|acquired|to\s+buy|buys|agrees\s+to\s+acquire|'
    r'pursu(?:es|ing)|bids?\s+for|offers?\s+to\s+(?:buy|acquire)|'
    r'in\s+talks\s+to\s+(?:buy|acquire)|weighs\s+(?:a\s+)?(?:bid|offer)\s+for|'
    r'explores\s+(?:an\s+)?acquisition\s+of|launches\s+(?:a\s+)?(?:tender\s+)?offer\s+for|'
    r'proposes\s+to\s+acquire|'
    r'bets\s+\$?[\d.]+\s*[bmk]?(?:illion)?\s+on|makes?\s+\$?[\d.]+\s*[bmk]?\s+deal\s+for)\s+'
    r'([A-Z][\w.&-]*(?:\s+[A-Z][\w.&-]*){0,3})', re.IGNORECASE)

# Fallback patterns that identify only the TARGET. Handles consortium / bid
# phrasing where no "X acquires Y" pair exists: "KKR joins consortium pursuing
# Steadfast acquisition proposal" (2026-07-14: scored KKR as a top BULL).
_TARGET_AFTER_RE = re.compile(
    r'(?:pursuing|acquisition\s+of|takeover\s+of|buyout\s+of|bid\s+for|'
    r'offer\s+for|to\s+acquire|to\s+buy)\s+'
    r'([A-Z][\w.&-]*(?:\s+[A-Z][\w.&-]*){0,3})')
_TARGET_BEFORE_RE = re.compile(
    r'([A-Z][\w.&-]*(?:\s+[A-Z][\w.&-]*){0,3})\s+'
    r'(?:acquisition|takeover|buyout|merger)\s+(?:proposal|bid|offer|deal|talks)')


def ma_side(text: str, ticker: str) -> str:
    """Return 'acquirer', 'target' or '' for this ticker in an M&A headline."""
    low = text.lower()
    if not any(t in low for t in _MA_TRIGGERS):
        return ""
    name = (TU.company_name_for(ticker) or "").lower()
    key = _name_evidence(name)   # generic-safe ("united parcel", not "united")
    m = _ACQUIRER_RE.search(text)
    if m:
        buyer, target = m.group(1).lower(), m.group(2).lower()
        if len(key) >= 4:
            if key in buyer:
                return "acquirer"
            if key in target:
                return "target"
        if ticker.lower() in buyer:
            return "acquirer"
        if ticker.lower() in target:
            return "target"
        return ""

    # Fallback: find the TARGET by pattern. In an M&A headline, any OTHER
    # company mentioned is on the buy side (consortium member, bidder).
    m2 = _TARGET_BEFORE_RE.search(text) or _TARGET_AFTER_RE.search(text)
    if not m2:
        return ""
    tgt = m2.group(1).lower()
    if (len(key) >= 4 and key in tgt) or ticker.lower() in tgt:
        return "target"
    if (len(key) >= 4 and key in low) or \
            re.search(rf'\b{re.escape(ticker)}\b', text):
        return "acquirer"
    return ""


def score_text(text: str, ticker: str | None = None) -> float:
    lower = text.lower()
    score = 0.0
    for kw, w in CATALYST_WEIGHTS.items():
        if kw in lower:
            score += w

    if ticker:
        side = ma_side(text, ticker)
        if side == "acquirer":
            # Strip the M&A bull credit and apply the real historical drift.
            for kw in ("acquisition", "merger", "takeover", "buyout", "deal",
                       "agreement", "acquires", "acquired"):
                if kw in lower:
                    score -= CATALYST_WEIGHTS[kw]
            score -= 3.0          # acquirers typically trade DOWN on announcement
        elif side == "target":
            score += 15.0         # targets gap to the offer price. This is the trade.
    return score


# Company names that START with a generic word would corroborate falsely:
# "United Parcel Service" -> first word "united" matches "United States" in
# any macro headline and re-admits UPS; "First Bancorp" -> "first" matches
# almost anything. For these, require the first TWO words of the name.
GENERIC_NAME_WORDS = {
    "united", "first", "american", "national", "general", "global", "world",
    "international", "pacific", "southern", "northern", "western", "eastern",
    "digital", "capital", "consumer", "energy", "health", "home", "royal",
}


def _name_evidence(name: str) -> str:
    """Return the shortest company-name substring that is strong evidence the
    company is actually the subject of the text."""
    words = name.split()
    if not words:
        return ""
    if words[0] in GENERIC_NAME_WORDS and len(words) >= 2:
        return f"{words[0]} {words[1]}"          # "united parcel"
    return words[0] if len(words[0]) >= 4 else ""


def extract_tickers(text: str) -> list[str]:
    """
    Three-tier ticker extraction (fixes false positives like 'NFL' in an
    Eli Manning headline, or attributing a BitGo (BTGO) lawsuit to YORK):

      Tier 1: explicit syntax  ($AVGO, (NASDAQ: BTGO))  -> always accepted
      Tier 2: bare ALL-CAPS word -> accepted only if it is a real listed
              ticker (SEC universe) AND not an ambiguous English word
      Tier 3: ambiguous tickers (BULL, COCO, NOW...) -> accepted only when
              the company's actual name also appears in the text
    """
    found: set[str] = set()

    # Tier 1 — explicit syntax wins unconditionally
    for m in STRONG_TICKER_RE.finditer(text):
        tk = next(g for g in m.groups() if g)
        found.add(tk.split(".")[0])

    # Tier 2 / 3 — bare words, validated against the listed universe
    lower_text = text.lower()
    for m in TICKER_RE.finditer(text):
        t = m.group(1)
        if t in found or t in EXCLUDE_WORDS:
            continue
        if not TU.is_valid_ticker(t):
            continue  # not a real listed ticker -> drop (kills YORK, NFL, RCX noise)
        if _needs_corroboration(t):
            # Real ticker, but also a country / commodity / macro term / suffix /
            # English word. Only accept if the company's actual name is present.
            name = (TU.company_name_for(t) or "").lower()
            ev = _name_evidence(name)
            if ev and ev in lower_text:
                found.add(t)
            continue
        found.add(t)

    return list(found)


def is_recent(entry_time, hours=LOOKBACK_HOURS) -> bool:
    """FAIL CLOSED. Previously returned True when the timestamp was missing or
    unparseable, so any undated feed item was 'fresh' forever. That is how a
    July 6 VRTX/Crinetics story and a WDFC gap from the prior session were
    still being scored as overnight catalysts on July 13."""
    if not entry_time:
        return False
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    try:
        if hasattr(entry_time, 'timetuple') or isinstance(entry_time, tuple):
            entry_dt = datetime(*entry_time[:6], tzinfo=timezone.utc)
        else:
            entry_dt = entry_time
        if entry_dt.tzinfo is None:
            entry_dt = entry_dt.replace(tzinfo=timezone.utc)
        return entry_dt >= cutoff
    except Exception:
        return False


_EN_STOPWORDS = {
    "the", "of", "to", "in", "for", "and", "on", "with", "as", "at", "by",
    "from", "after", "amid", "its", "is", "are", "up", "down", "over", "new",
    "will", "has", "have", "an", "a", "into", "than", "more", "says", "said",
}


def is_probably_english(s: str) -> bool:
    """Cheap language gate for PR-wire noise. Two signals:
      1) letters >10% non-ASCII (Cyrillic, heavy-diacritic text), or
      2) accented letters present AND zero common English function words
         (catches Lithuanian/Baltic PR, which uses few diacritics -- the
         2026-07-14 'UAB Atsinaujinancios energetikos...' item passed a pure
         ratio test at ~3% non-ASCII).
    Pure-ASCII text always passes."""
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return True
    non_ascii = sum(1 for c in letters if ord(c) > 127)
    if (non_ascii / len(letters)) >= 0.10:
        return False
    if non_ascii >= 2:
        words = re.findall(r"[a-z']+", s.lower())
        if len(words) >= 5 and not (_EN_STOPWORDS & set(words)):
            return False
    return True


def clean_text(s: str) -> str:
    """Remove non-ASCII and control characters from scraped text."""
    return s.encode("ascii", errors="replace").decode("ascii").replace("?", " ").strip()


def fetch(url, timeout=REQUEST_TIMEOUT):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"  {Fore.RED}[WARN] {url[:60]}... -> {e}{Style.RESET_ALL}")
        return None


# --- SCRAPERS ----------------------------------------------------------------

def scrape_rss(feed_url: str, source_name: str) -> list[dict]:
    """Generic RSS scraper.

    Fetches the feed with requests (hard timeout via fetch()) and then hands
    the bytes to feedparser. feedparser.parse(url) does its own download with
    NO timeout, which is what caused runs to hang for 10+ minutes whenever a
    feed (Seeking Alpha, Investing.com, etc.) stalled. Worst case now is
    REQUEST_TIMEOUT seconds per dead feed, then we skip it.
    """
    items = []
    try:
        r = fetch(feed_url)
        if r is None:
            return items  # feed down/blocked -> already warned in fetch(), skip
        feed = feedparser.parse(r.content)
        for entry in feed.entries:
            pub = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
            if not is_recent(pub):
                continue
            title = getattr(entry, "title", "")
            summary = getattr(entry, "summary", "")
            text = f"{title} {summary}"
            items.append({"source": source_name, "title": title, "text": text, "pub": pub})
    except Exception as e:
        print(f"  {Fore.RED}[WARN] RSS {source_name}: {e}{Style.RESET_ALL}")
    return items


def scrape_yahoo_movers() -> list[dict]:
    """Yahoo Finance pre-market gainers/losers."""
    items = []
    urls = [
        ("https://finance.yahoo.com/markets/stocks/gainers/", "Yahoo Gainers"),
        ("https://finance.yahoo.com/markets/stocks/losers/", "Yahoo Losers"),
        ("https://finance.yahoo.com/markets/stocks/most-active/", "Yahoo Most Active"),
    ]
    for url, name in urls:
        r = fetch(url)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "lxml")
        rows = soup.select("tr")
        for row in rows[:30]:
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            ticker_cell = cells[0].get_text(strip=True)
            # Yahoo format: AAPL Apple Inc.
            ticker_match = re.match(r'^([A-Z]{1,5})', ticker_cell)
            if ticker_match:
                tk = ticker_match.group(1)
                if tk not in EXCLUDE_WORDS:
                    change_text = cells[4].get_text(strip=True) if len(cells) > 4 else ""
                    items.append({
                        "source": name,
                        "title": f"{tk} on {name} list ({change_text})",
                        "text": f"{tk} {name} pre-market mover {change_text}",
                        "pub": None,
                        "forced_ticker": tk,
                        # Gainers/Losers are directional; Most Active is NOT —
                        # it confirms a name is moving (via mover_sources) but
                        # must not inject bullish score on its own.
                        "forced_score": 8 if "Gain" in name else (-6 if "Los" in name else 0),
                        "is_mover": True,
                    })
    return items


def scrape_benzinga_rss() -> list[dict]:
    feeds = [
        ("https://www.benzinga.com/feed", "Benzinga"),
    ]
    items = []
    for url, name in feeds:
        items += scrape_rss(url, name)
    return items


def scrape_yahoo_rss() -> list[dict]:
    feeds = [
        ("https://finance.yahoo.com/news/rssindex", "Yahoo Finance RSS"),
        ("https://finance.yahoo.com/rss/topfinstories", "Yahoo Top Finance"),
    ]
    items = []
    for url, name in feeds:
        items += scrape_rss(url, name)
    return items


def scrape_marketwatch_rss() -> list[dict]:
    feeds = [
        ("https://feeds.content.dowjones.io/public/rss/mw_topstories", "MarketWatch Top"),
        ("https://feeds.content.dowjones.io/public/rss/mw_marketpulse", "MarketWatch Pulse"),
        ("https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines", "MarketWatch RT"),
    ]
    items = []
    for url, name in feeds:
        items += scrape_rss(url, name)
    return items


def scrape_seeking_alpha_rss() -> list[dict]:
    feeds = [
        ("https://seekingalpha.com/market_currents.xml", "Seeking Alpha Currents"),
        ("https://seekingalpha.com/feed.xml", "Seeking Alpha"),
    ]
    items = []
    for url, name in feeds:
        items += scrape_rss(url, name)
    return items


def scrape_investing_com_rss() -> list[dict]:
    feeds = [
        ("https://www.investing.com/rss/news.rss", "Investing.com News"),
        ("https://www.investing.com/rss/news_25.rss", "Investing.com US Stocks"),
        ("https://www.investing.com/rss/news_14.rss", "Investing.com Earnings"),
        ("https://www.investing.com/rss/news_1.rss", "Investing.com Economy"),
    ]
    items = []
    for url, name in feeds:
        items += scrape_rss(url, name)
    return items


def scrape_finviz_news() -> list[dict]:
    """Finviz news page - text scrape."""
    items = []
    r = fetch("https://finviz.com/news.ashx")
    if not r:
        return items
    soup = BeautifulSoup(r.text, "lxml")
    table = soup.find("table", class_="news_time-table")
    if not table:
        return items
    rows = table.find_all("tr")[1:]  # skip header row
    for row in rows[:80]:
        link = row.find("a")
        if not link:
            continue
        title = link.get_text(strip=True)
        if not title or len(title) < 8:
            continue
        items.append({"source": "Finviz", "title": title, "text": title, "pub": None})
    return items


def scrape_finviz_movers() -> list[dict]:
    """
    Finviz screener: top gainers and losers (structured table -> real tickers).
    NOTE: Finviz sometimes 403s datacenter IPs; fails gracefully if blocked.
    """
    items = []
    screens = [
        ("https://finviz.com/screener.ashx?v=111&s=ta_topgainers&f=sh_avgvol_o500,sh_price_o5",
         "Finviz Top Gainers", 7),
        ("https://finviz.com/screener.ashx?v=111&s=ta_toplosers&f=sh_avgvol_o500,sh_price_o5",
         "Finviz Top Losers", -6),
    ]
    for url, name, base in screens:
        r = fetch(url)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "lxml")
        # screener ticker links look like: quote.ashx?t=AVGO
        seen = set()
        for a in soup.select('a[href*="quote.ashx?t="]'):
            tk = a.get_text(strip=True).upper()
            if not tk or tk in seen or len(tk) > 5 or not tk.isalpha():
                continue
            seen.add(tk)
            items.append({
                "source": name,
                "title": f"{tk} on {name}",
                "text": f"{tk} {name} pre-market mover",
                "pub": None,
                "forced_ticker": tk,
                "forced_score": base,
                "is_mover": True,
            })
            if len(seen) >= 20:
                break
    return items


def scrape_stocktwits_trending() -> list[dict]:
    """
    StockTwits trending symbols — free public JSON API, returns clean tickers.
    Trending on StockTwits pre-market = retail attention = intraday range.
    """
    items = []
    r = fetch("https://api.stocktwits.com/api/2/trending/symbols.json")
    if not r:
        return items
    try:
        data = r.json()
        for sym in data.get("symbols", [])[:25]:
            tk = str(sym.get("symbol", "")).upper()
            title = str(sym.get("title", ""))
            if not tk or not tk.isalpha() or len(tk) > 5:
                continue
            items.append({
                "source": "StockTwits Trending",
                "title": f"{tk} trending on StockTwits ({title})",
                "text": f"{tk} {title} trending social volume",
                "pub": None,
                "forced_ticker": tk,
                "forced_score": 5,
                "is_mover": True,
            })
    except Exception as e:
        print(f"  {Fore.RED}[WARN] StockTwits parse: {e}{Style.RESET_ALL}")
    return items


def scrape_globenewswire_rss() -> list[dict]:
    """GlobeNewswire — primary press-release wire (earnings, FDA, contracts)."""
    feeds = [
        ("https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/"
         "GlobeNewswire%20-%20News%20about%20Public%20Companies",
         "GlobeNewswire"),
    ]
    items = []
    for url, name in feeds:
        items += scrape_rss(url, name)
    return items


def scrape_accesswire_rss() -> list[dict]:
    """Accesswire — press-release wire popular with small caps (your gappers)."""
    feeds = [
        ("https://www.accesswire.com/rss/latest", "Accesswire"),
    ]
    items = []
    for url, name in feeds:
        items += scrape_rss(url, name)
    return items


def scrape_nasdaq_rss() -> list[dict]:
    """Nasdaq.com markets feed."""
    feeds = [
        ("https://www.nasdaq.com/feed/rssoutbound?category=Markets", "Nasdaq Markets"),
        ("https://www.nasdaq.com/feed/rssoutbound?category=Earnings", "Nasdaq Earnings"),
    ]
    items = []
    for url, name in feeds:
        items += scrape_rss(url, name)
    return items


def scrape_finnhub_news() -> list[dict]:
    """
    Optional: Finnhub general news (free tier, real-time). Activates only if
    FINNHUB_API_KEY is set as a repo secret / env var. Good real-time overlay
    on top of your delayed Polygon data.
    """
    items = []
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not key:
        return items
    r = fetch(f"https://finnhub.io/api/v1/news?category=general&token={key}")
    if not r:
        return items
    try:
        for art in r.json()[:60]:
            ts = art.get("datetime")
            pub = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
            if pub and not is_recent(pub):
                continue
            title = art.get("headline", "")
            summary = art.get("summary", "")
            related = str(art.get("related", "")).upper()  # Finnhub gives tickers!
            text = f"{title} {summary} {related}"
            item = {"source": "Finnhub", "title": title, "text": text, "pub": pub}
            # Finnhub's 'related' field is an authoritative ticker tag
            rel = [t for t in related.split(",") if t and t.isalpha() and len(t) <= 5]
            if len(rel) == 1:
                item["forced_ticker"] = rel[0]
                item["forced_score"] = score_text(text)
            items.append(item)
    except Exception as e:
        print(f"  {Fore.RED}[WARN] Finnhub parse: {e}{Style.RESET_ALL}")
    return items


def scrape_stockanalysis_news() -> list[dict]:
    """StockAnalysis.com news feed."""
    items = []
    r = fetch("https://stockanalysis.com/news/")
    if not r:
        return items
    soup = BeautifulSoup(r.text, "lxml")
    seen = set()
    for tag in soup.find_all("h3")[:60]:
        title = tag.get_text(strip=True)
        if len(title) < 10 or title in seen:
            continue
        seen.add(title)
        items.append({"source": "StockAnalysis", "title": title, "text": title, "pub": None})
    return items


def scrape_market_chameleon_earnings() -> list[dict]:
    """MarketChameleon earnings calendar for today."""
    items = []
    today = now_ct().strftime("%Y-%m-%d")
    url = f"https://marketchameleon.com/Overview/Earnings?date={today}"
    r = fetch(url)
    if not r:
        return items
    soup = BeautifulSoup(r.text, "lxml")
    for row in soup.select("table tbody tr")[:50]:
        cells = row.find_all("td")
        if not cells:
            continue
        ticker_el = cells[0].find("a") or cells[0]
        ticker = ticker_el.get_text(strip=True).upper()
        if not ticker or ticker in EXCLUDE_WORDS or len(ticker) > 5:
            continue
        # Get eps/revenue data if available
        extra = " ".join(c.get_text(strip=True) for c in cells[1:6])
        items.append({
            "source": "MarketChameleon Earnings",
            "title": f"{ticker} earnings report today",
            "text": f"{ticker} earnings report {extra}",
            "pub": None,
            "forced_ticker": ticker,
            "forced_score": 9,
        })
    return items


def scrape_market_chameleon_ipos() -> list[dict]:
    """MarketChameleon IPO calendar."""
    items = []
    r = fetch("https://marketchameleon.com/IPOs/")
    if not r:
        return items
    soup = BeautifulSoup(r.text, "lxml")
    for row in soup.select("table tbody tr")[:20]:
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        ticker_el = cells[0].find("a") or cells[0]
        ticker = ticker_el.get_text(strip=True).upper()
        if not ticker or ticker in EXCLUDE_WORDS or len(ticker) > 5:
            continue
        items.append({
            "source": "MarketChameleon IPO",
            "title": f"{ticker} IPO / recent listing",
            "text": f"{ticker} IPO listing new public company",
            "pub": None,
            "forced_ticker": ticker,
            "forced_score": 10,
        })
    return items


def scrape_pr_newswire_rss() -> list[dict]:
    feeds = [
        ("https://www.prnewswire.com/rss/news-releases-list.rss", "PR Newswire"),
    ]
    items = []
    for url, name in feeds:
        items += scrape_rss(url, name)
    return items


def scrape_cnbc_rss() -> list[dict]:
    feeds = [
        ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362", "CNBC Markets"),
        ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147", "CNBC Finance"),
    ]
    items = []
    for url, name in feeds:
        items += scrape_rss(url, name)
    return items


def scrape_sec_edgar() -> list[dict]:
    """SEC EDGAR 8-K filings (material events). SEC requires a descriptive User-Agent."""
    items = []
    sec_headers = {"User-Agent": "Mozilla/5.0 premarket-scanner research@example.com"}
    url = ("https://www.sec.gov/cgi-bin/browse-edgar"
           "?action=getcurrent&type=8-K&dateb=&owner=include&count=40&output=atom")
    try:
        r = requests.get(url, headers=sec_headers, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        import feedparser as _fp
        feed = _fp.parse(r.text)
        for entry in feed.entries:
            pub = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
            if not is_recent(pub):
                continue
            title = getattr(entry, "title", "")
            item = {"source": "SEC 8-K Filings", "title": title, "text": title, "pub": pub}
            # The EDGAR getcurrent index title is literally
            #   "8-K - TRICO BANCSHARES (0000356171) (Filer)"
            # There is NO event content in it, so score_text() returns 0 and the
            # old flat forced_score=6 produced a wall of identical +8.9 rows for
            # every company that filed anything. Worse, it swallowed the real
            # FHB/TCBK merger 8-K into that generic bucket.
            #
            # An 8-K is now CORROBORATION ONLY: it can confirm a ticker that
            # already has a real catalyst, but it can never create a row by itself.
            name_hits = TU.tickers_from_company_names(title, max_hits=1)
            if len(name_hits) == 1:
                item["forced_ticker"] = next(iter(name_hits))
                item["forced_score"] = 0.0
                item["corroboration_only"] = True
            items.append(item)
    except Exception as e:
        print(f"  [WARN] SEC EDGAR: {e}")
    return items


# --- AGGREGATION -------------------------------------------------------------

def run_all_scrapers() -> list[dict]:
    scrapers = [
        ("Yahoo Finance RSS", scrape_yahoo_rss),
        ("Yahoo Movers", scrape_yahoo_movers),
        ("MarketWatch RSS", scrape_marketwatch_rss),
        ("Benzinga RSS", scrape_benzinga_rss),
        ("Seeking Alpha RSS", scrape_seeking_alpha_rss),
        ("Investing.com RSS", scrape_investing_com_rss),
        ("Finviz News", scrape_finviz_news),
        ("Finviz Movers", scrape_finviz_movers),
        ("StockTwits Trending", scrape_stocktwits_trending),
        ("GlobeNewswire", scrape_globenewswire_rss),
        ("Accesswire", scrape_accesswire_rss),
        ("Nasdaq RSS", scrape_nasdaq_rss),
        ("Finnhub (optional)", scrape_finnhub_news),
        ("StockAnalysis", scrape_stockanalysis_news),
        ("MarketChameleon Earnings", scrape_market_chameleon_earnings),
        ("MarketChameleon IPOs", scrape_market_chameleon_ipos),
        ("PR Newswire", scrape_pr_newswire_rss),
        ("CNBC RSS", scrape_cnbc_rss),
        ("SEC 8-K", scrape_sec_edgar),
    ]
    all_items = []
    for name, fn in scrapers:
        print(f"  {Fore.CYAN}Scraping {name}...{Style.RESET_ALL}", end=" ", flush=True)
        try:
            results = fn()
            print(f"{Fore.GREEN}{len(results)} items{Style.RESET_ALL}")
            all_items.extend(results)
        except Exception as e:
            print(f"{Fore.RED}ERROR: {e}{Style.RESET_ALL}")
    return all_items


def aggregate_scores(items: list[dict]) -> dict:
    """
    Returns dict: ticker -> {score, mentions, sources, headlines, catalyst_type,
                             news_sources, mover_sources}
    """
    ticker_data = defaultdict(lambda: {
        "score": 0.0,
        "mentions": 0,
        "sources": set(),
        "headlines": [],
        "catalyst_type": set(),
        "news_sources": set(),   # real reporting (RSS, wires, SEC)
        "mover_sources": set(),  # lists of yesterday's movers (Yahoo/Finviz/StockTwits)
        "real_evidence": 0,      # items that are NOT corroboration-only 8-K noise
        "scored_headlines": [],  # (abs_score, headline) -> lets us show the DRIVER
    })

    # Dedupe syndicated copies of the same story (same headline via 3 feeds
    # should not count as 3 mentions). Key = first 60 alphanumeric chars.
    seen_keys: set[str] = set()
    deduped = []
    for item in items:
        key = re.sub(r'[^a-z0-9]', '', item.get("title", "").lower())[:60]
        if key and not item.get("is_mover"):
            if key in seen_keys:
                continue
            seen_keys.add(key)
        deduped.append(item)
    items = deduped

    # Drop plaintiff-firm advertising BEFORE it reaches the scorer. Must run
    # after dedupe (each firm writes its own headline, so dedupe cannot catch
    # them) and before scoring (or the M&A target bonus fires on merger-
    # objection ads). is_mover items are structured rows, never PR text -- skip.
    kept, spam_n = [], 0
    for item in items:
        if item.get("is_mover"):
            kept.append(item)
            continue
        blob = f"{item.get('title', '')} {item.get('text', '')}"
        if is_law_firm_spam(blob):
            spam_n += 1
            continue
        # 2026-07-14: a Lithuanian-language UAB press release ("Atsinaujinancios
        # energetikos investicijos...") scored AEI. Non-English PR is noise for
        # a US pre-market scan -- drop it before extraction.
        if not is_probably_english(blob):
            spam_n += 1
            continue
        kept.append(item)
    if spam_n:
        print(f"  {Fore.YELLOW}[SPAM] Dropped {spam_n} law-firm / investor-alert "
              f"PR items ({len(kept)} kept){Style.RESET_ALL}")
    items = kept

    for item in items:
        text = clean_text(item.get("text", "") + " " + item.get("title", ""))
        item["title"] = clean_text(item.get("title", ""))
        base_score = score_text(text)   # ticker-agnostic baseline

        # Determine catalyst types
        cats = set()
        lower = text.lower()
        if any(k in lower for k in ["earnings", "eps", "revenue", "guidance"]):
            cats.add("Earnings")
        if any(k in lower for k in ["fda", "approval", "clinical", "phase"]):
            cats.add("FDA/Biotech")
        # 2026-07-14: "First Bancorp To Acquire First Carolina" and "Diodes To
        # Buy ElevATE" were labeled "General" -- only noun forms were checked.
        if any(k in lower for k in ["acquisition", "merger", "buyout", "takeover",
                                    "acquire", "acquires", "acquired", "to buy",
                                    "buys "]):
            cats.add("M&A")
        if any(k in lower for k in ["upgrade", "downgrade", "price target", "initiated"]):
            cats.add("Analyst")
        if any(k in lower for k in ["contract", "partnership", "collaboration", "awarded"]):
            cats.add("Contract")
        if any(k in lower for k in ["short squeeze", "heavily shorted"]):
            cats.add("Squeeze")
        if any(k in lower for k in ["ipo", "listing", "spac"]):
            cats.add("IPO")
        if any(k in lower for k in ["bankruptcy", "fraud", "sec investigation", "class action"]):
            cats.add("Risk")
        if any(k in lower for k in ["mover", "gainer", "loser", "most active"]):
            cats.add("Mover")
        if not cats:
            cats.add("General")

        # Forced ticker from structured data (Yahoo movers, MC earnings, etc.)
        if "forced_ticker" in item:
            tk = item["forced_ticker"]
            forced_s = item.get("forced_score", base_score)
            d = ticker_data[tk]
            ticker_score = score_text(text, tk)          # M&A-aware
            contrib = forced_s + ticker_score * 0.3
            d["score"] += contrib
            d["mentions"] += 1
            d["sources"].add(item["source"])
            if item.get("is_mover"):
                d["mover_sources"].add(item["source"])
            else:
                d["news_sources"].add(item["source"])
            if not item.get("corroboration_only"):
                d["real_evidence"] += 1
            d["catalyst_type"].update(cats)
            if item["title"] not in d["headlines"]:
                d["headlines"].append(item["title"])
                d["scored_headlines"].append((abs(contrib), item["title"]))
            continue

        # Extract tickers from text
        tickers = extract_tickers(text)
        if not tickers:
            continue

        for tk in tickers:
            # The bank is the AUTHOR of the analyst action, not the subject
            # ("HSBC loses conviction in leading drugmaker" is not HSBC news).
            # Real GS/MS/etc. moves still get in via the Yahoo movers path.
            if tk in SELL_SIDE_BANKS and ANALYST_ACTION_RE.search(text):
                continue
            d = ticker_data[tk]
            # Extraction is now universe-validated, so full credit; small extra
            # weight for household names the keyword engine understands well.
            multiplier = 1.0 if tk in KNOWN_TICKERS else 0.8
            contrib = score_text(text, tk) * multiplier   # M&A-aware per ticker
            d["score"] += contrib
            d["mentions"] += 1
            d["sources"].add(item["source"])
            d["news_sources"].add(item["source"])
            d["real_evidence"] += 1
            d["catalyst_type"].update(cats)
            if item["title"] not in d["headlines"] and len(d["headlines"]) < 3:
                d["headlines"].append(item["title"])
                d["scored_headlines"].append((abs(contrib), item["title"]))

    return ticker_data


def apply_rules_flags(ranked: list[tuple]) -> None:
    """Tag tickers that violate the trading-rules universe (price > $5,
    market cap > $500M) so they are visibly NO-TRADE rows. 2026-07-14: PSHG
    (sub-$5 shipping micro-cap) ranked #6 with no warning.

    Uses stockanalysis.com's quote API (no key). FAILS OPEN: if the lookup
    breaks, no flags are added and the scan is unaffected. Hard 45-second
    total budget so 30 slow lookups can never stall the email."""
    SKIP = {"SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "TLT", "VIX"}  # not stocks
    deadline = time.time() + 45
    for tk, _score, d in ranked:
        if tk in SKIP or time.time() > deadline:
            continue
        try:
            r = requests.get(
                f"https://stockanalysis.com/api/symbol/s/{tk.lower()}/quote",
                headers=HEADERS, timeout=5)
            if r.status_code != 200:
                continue
            q = r.json().get("data", {}) or {}
            price = q.get("p") or q.get("price")
            mcap = q.get("mc") or q.get("marketCap")
            flags = []
            if isinstance(price, (int, float)) and price < 5:
                flags.append(f"<$5 (${price:.2f})")
            if isinstance(mcap, (int, float)) and 0 < mcap < 500_000_000:
                flags.append("<$500M cap")
            if flags:
                d["rule_flags"] = " / ".join(flags)
        except Exception:
            continue  # fail open -- never let a quote lookup kill the scan


def rank_tickers(ticker_data: dict, top_n=TOP_N) -> list[tuple]:
    """
    Score formula:
      final = raw_score + mention_bonus + source_diversity_bonus + known_bonus
              + CONFIRMED bonus (news AND mover-list -> the real gap+catalyst setup)
      mover-list-only tickers are capped: appearing on yesterday's gainers list
      is lagging information, not a catalyst.
    Adds d['confirmation'] in {'CONFIRMED','NEWS','MOVER-ONLY'}.
    """
    import math
    MOVER_ONLY_CAP = 6.0      # mover-only names can never outrank real catalysts
    CONFIRM_BONUS = 6.0       # news + on movers list = catalyst already moving

    ranked = []
    for tk, d in ticker_data.items():
        if d["mentions"] < 1:
            continue
        # A bare 8-K filing notice is not a catalyst. If ALL evidence for this
        # ticker was corroboration-only, it never earns a row.
        if d.get("real_evidence", 0) == 0:
            continue
        # Show the headline that actually DROVE the score, not whichever feed
        # happened to arrive first (that is why every top row said
        # "PLUG on Yahoo Most Active list").
        if d.get("scored_headlines"):
            d["headlines"] = [h for _, h in sorted(d["scored_headlines"], reverse=True)]
        has_news = len(d["news_sources"]) > 0
        has_mover = len(d["mover_sources"]) > 0

        raw = d["score"]
        # Evidence bonuses measure STRENGTH of signal, not direction. They must
        # amplify the raw score's direction, not always push bullish — otherwise
        # a stock down -17% on Yahoo Losers (raw ~ -1) gets buried under +10 of
        # unsigned bonuses and ranks as a top BULL (the ORCL/ADBE bug).
        sign = 1.0 if raw > 0 else (-1.0 if raw < 0 else 0.0)
        mention_bonus = math.log1p(d["mentions"]) * 2
        source_bonus = len(d["sources"]) * 1.5
        known_bonus = 3.0 if tk in KNOWN_TICKERS else 0.0
        final = raw + sign * (mention_bonus + source_bonus + known_bonus)

        if has_news and has_mover:
            d["confirmation"] = "CONFIRMED"
            final += sign * CONFIRM_BONUS
        elif has_news:
            d["confirmation"] = "NEWS"
        else:
            d["confirmation"] = "MOVER-ONLY"
            # Lagging signal: cap it so it sorts below anything with a catalyst
            final = max(min(final, MOVER_ONLY_CAP), -MOVER_ONLY_CAP)

        ranked.append((tk, final, d))

    # Sort: confirmed first, then by absolute score (big moves either way are tradeable)
    conf_order = {"CONFIRMED": 0, "NEWS": 1, "MOVER-ONLY": 2}
    ranked.sort(key=lambda x: (conf_order[x[2]["confirmation"]], -abs(x[1])))
    return ranked[:top_n]


# --- DISPLAY -----------------------------------------------------------------

def direction_emoji(score):
    if score >= 10:
        return f"{Fore.GREEN}^^ BULLISH{Style.RESET_ALL}"
    elif score >= 4:
        return f"{Fore.GREEN}^ Bull{Style.RESET_ALL}"
    elif score <= -10:
        return f"{Fore.RED}vv BEARISH{Style.RESET_ALL}"
    elif score <= -4:
        return f"{Fore.RED}v Bear{Style.RESET_ALL}"
    else:
        return f"{Fore.YELLOW}* Neutral{Style.RESET_ALL}"


def print_results(ranked: list[tuple]):
    now = now_ct().strftime("%Y-%m-%d %H:%M:%S")
    print()
    print(f"{Fore.YELLOW}{'='*90}")
    print(f"  PRE-MARKET CATALYST SCANNER  |  {now}")
    print(f"  Showing top {len(ranked)} stocks ranked by move potential (scalping focus)")
    print(f"{'='*90}{Style.RESET_ALL}")
    print()

    rows = []
    for rank, (tk, score, d) in enumerate(ranked, 1):
        catalyst_str = ", ".join(sorted(d["catalyst_type"]))
        source_str = ", ".join(sorted(d["sources"]))[:50]
        headline = (d["headlines"][0][:65] + "...") if d["headlines"] else "--"
        direction = direction_emoji(score)
        score_display = f"{score:+.1f}"
        rows.append([
            rank,
            f"{Fore.WHITE}{Style.BRIGHT}{tk}{Style.RESET_ALL}",
            score_display,
            direction,
            d["mentions"],
            len(d["sources"]),
            catalyst_str[:28],
            headline,
        ])

    print(tabulate(
        rows,
        headers=["Rank", "Ticker", "Score", "Bias", "Mentions", "Sources", "Catalyst", "Top Headline"],
        tablefmt="simple",
        colalign=("right", "left", "right", "left", "right", "right", "left", "left"),
    ))

    print()
    print(f"{Fore.CYAN}DETAILED BREAKDOWN (Top 10):{Style.RESET_ALL}")
    print()
    for rank, (tk, score, d) in enumerate(ranked[:10], 1):
        bias = "BULLISH" if score > 0 else "BEARISH"
        color = Fore.GREEN if score > 0 else Fore.RED
        print(f"  {color}{Style.BRIGHT}#{rank}  {tk}  ({score:+.1f})  [{bias}]{Style.RESET_ALL}")
        print(f"      Catalyst : {', '.join(sorted(d['catalyst_type']))}")
        print(f"      Mentions : {d['mentions']}  |  Sources: {len(d['sources'])}")
        print(f"      Sources  : {', '.join(sorted(d['sources']))[:70]}")
        for hl in d["headlines"][:3]:
            print(f"      * {hl[:80]}")
        print()


def print_scalp_watchlist(ranked: list[tuple]):
    """Compact watchlist output suitable for a trading terminal."""
    print(f"{Fore.YELLOW}SCALP WATCHLIST (copy-paste ready):{Style.RESET_ALL}")
    long_side  = [tk for tk, s, d in ranked if s >= 5][:10]
    short_side = [tk for tk, s, d in ranked if s <= -5][:10]
    neutral    = [tk for tk, s, d in ranked if -5 < s < 5][:5]

    if long_side:
        print(f"  {Fore.GREEN}LONG BIAS  : {', '.join(long_side)}{Style.RESET_ALL}")
    if short_side:
        print(f"  {Fore.RED}SHORT BIAS : {', '.join(short_side)}{Style.RESET_ALL}")
    if neutral:
        print(f"  {Fore.YELLOW}NEUTRAL    : {', '.join(neutral)}{Style.RESET_ALL}")
    print()


# --- EMAIL -------------------------------------------------------------------

def build_email_body(ranked: list[tuple]) -> tuple[str, str]:
    """Return (plain_text, html) email body."""
    now_str = now_ct().strftime("%A %B %d, %Y  %I:%M %p CT")
    scan_date = now_ct().strftime("%Y-%m-%d")

    long_side  = [tk for tk, s, d in ranked if s >= 5][:12]
    short_side = [tk for tk, s, d in ranked if s <= -5][:8]

    # Portfolio discipline section (safe if module or CSV is missing)
    portfolio_plain, portfolio_html, portfolio_flags = "", "", {}
    if _portfolio is not None:
        try:
            all_tickers = [tk for tk, _s, _d in ranked]
            portfolio_plain, portfolio_html, portfolio_flags = \
                _portfolio.build_discipline_section(watchlist_tickers=all_tickers)
        except Exception as e:
            print(f"[PORTFOLIO] discipline section failed: {e}")

    # ── PLAIN TEXT ──────────────────────────────────────────────────────────
    lines = []
    lines.append(f"PRE-MARKET CATALYST SCAN  --  {now_str}")
    lines.append("=" * 72)
    lines.append("")
    lines.append(f"LONG BIAS  : {', '.join(long_side) if long_side else 'none'}")
    lines.append(f"SHORT BIAS : {', '.join(short_side) if short_side else 'none'}")
    lines.append("")
    lines.append("FULL RANKED LIST (best -> worst by move potential):")
    lines.append("-" * 72)
    lines.append(f"{'#':<4} {'TICKER':<7} {'SCORE':>7}  {'BIAS':<8}  {'CATALYST':<22}  HEADLINE")
    lines.append("-" * 72)
    for rank, (tk, score, d) in enumerate(ranked, 1):
        bias = "BULL" if score > 0 else "BEAR"
        conf = d.get("confirmation", "")[:9]
        cat  = ", ".join(sorted(d["catalyst_type"]))[:20]
        hl   = (d["headlines"][0][:50] + "...") if d["headlines"] else ""
        rf   = f"  [NO-TRADE: {d['rule_flags']}]" if d.get("rule_flags") else ""
        lines.append(f"#{rank:<3} {tk:<7}{rf} {score:>+7.1f}  {bias:<5} {conf:<10} {cat:<22}  {hl}")
    lines.append("")
    lines.append("-" * 72)
    lines.append("DETAILED NOTES (top 15):")
    lines.append("")
    for rank, (tk, score, d) in enumerate(ranked[:15], 1):
        bias = "BULLISH" if score > 0 else "BEARISH"
        lines.append(f"#{rank}  {tk}  ({score:+.1f})  [{bias}]")
        lines.append(f"   Catalyst : {', '.join(sorted(d['catalyst_type']))}")
        lines.append(f"   Sources  : {', '.join(sorted(d['sources']))[:65]}")
        for hl in d["headlines"][:2]:
            lines.append(f"   * {hl[:75]}")
        lines.append("")
    lines.append("=" * 72)
    lines.append("Generated by Pre-Market Catalyst Scanner")
    if portfolio_plain:
        lines.append("")
        lines.append(portfolio_plain)
    plain = "\n".join(lines)

    # ── HTML ────────────────────────────────────────────────────────────────
    def row_color(score):
        if score >= 10:  return "#d4edda"   # bright green
        if score >= 4:   return "#eaf6ec"   # light green
        if score <= -10: return "#f8d7da"   # bright red
        if score <= -4:  return "#fdecea"   # light red
        return "#fff9e6"                    # yellow neutral

    def bias_badge(score):
        if score >= 10:  return '<span style="color:#155724;font-weight:bold">^^ BULL</span>'
        if score >= 4:   return '<span style="color:#28a745">^ Bull</span>'
        if score <= -10: return '<span style="color:#721c24;font-weight:bold">vv BEAR</span>'
        if score <= -4:  return '<span style="color:#dc3545">v Bear</span>'
        return '<span style="color:#856404">Neutral</span>'

    def flag_badge_html(tk):
        fs = portfolio_flags.get(tk)
        if not fs:
            return ""
        colors = {"HELD": "#dc3545", "CORE": "#17a2b8"}
        spans = []
        for f in fs:
            c = "#fd7e14" if f.startswith("COOLDOWN") else colors.get(f, "#6c757d")
            spans.append(
                f'<span style="background:{c};color:#fff;font-size:9px;'
                f'padding:1px 5px;border-radius:3px;margin-left:4px;'
                f'font-weight:normal">{f}</span>'
            )
        return "".join(spans)

    rows_html = ""
    for rank, (tk, score, d) in enumerate(ranked, 1):
        bg  = row_color(score)
        if d.get("rule_flags"):
            bg = "#e9ecef"   # grey out rule violators -- visibly NO-TRADE
        cat = ", ".join(sorted(d["catalyst_type"]))[:28]
        hl  = (d["headlines"][0][:65] + "...") if d["headlines"] else ""
        conf = d.get("confirmation", "")
        conf_colors = {"CONFIRMED": "#155724", "NEWS": "#0c5460", "MOVER-ONLY": "#999"}
        conf_html = (f'<span style="color:{conf_colors.get(conf, "#999")};'
                     f'font-weight:{"bold" if conf == "CONFIRMED" else "normal"};'
                     f'font-size:11px">{conf}</span>')
        rows_html += f"""
        <tr style="background:{bg}">
          <td style="padding:4px 8px;text-align:right">{rank}</td>
          <td style="padding:4px 8px;font-weight:bold;font-size:15px">{tk}{flag_badge_html(tk)}{('<span style="background:#6c757d;color:#fff;font-size:9px;padding:1px 5px;border-radius:3px;margin-left:4px;font-weight:normal">NO-TRADE ' + d["rule_flags"] + '</span>') if d.get("rule_flags") else ''}</td>
          <td style="padding:4px 8px;text-align:right">{score:+.1f}</td>
          <td style="padding:4px 8px">{bias_badge(score)}</td>
          <td style="padding:4px 8px">{conf_html}</td>
          <td style="padding:4px 8px;text-align:right">{d['mentions']}</td>
          <td style="padding:4px 8px;text-align:right">{len(d['sources'])}</td>
          <td style="padding:4px 8px">{cat}</td>
          <td style="padding:4px 8px;font-size:12px;color:#444">{hl}</td>
        </tr>"""

    long_html  = " &nbsp;|&nbsp; ".join(
        f'<span style="color:#155724;font-weight:bold">{t}</span>' for t in long_side
    ) or "<em>none</em>"
    short_html = " &nbsp;|&nbsp; ".join(
        f'<span style="color:#721c24;font-weight:bold">{t}</span>' for t in short_side
    ) or "<em>none</em>"

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:monospace;background:#f8f9fa;padding:20px">

  <div style="max-width:900px;margin:0 auto;background:#fff;border-radius:8px;
              padding:24px;box-shadow:0 2px 8px rgba(0,0,0,.12)">

    <h2 style="margin:0 0 4px;color:#1a1a2e">
      Pre-Market Catalyst Scanner
    </h2>
    <p style="margin:0 0 20px;color:#666;font-size:13px">{now_str}</p>

    <div style="background:#e8f4f8;border-left:4px solid #17a2b8;padding:12px 16px;
                border-radius:4px;margin-bottom:20px">
      <strong>LONG BIAS:</strong> {long_html}<br>
      <strong>SHORT BIAS:</strong> {short_html}
    </div>

    {portfolio_html}

    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead>
        <tr style="background:#343a40;color:#fff">
          <th style="padding:6px 8px;text-align:right">#</th>
          <th style="padding:6px 8px;text-align:left">Ticker</th>
          <th style="padding:6px 8px;text-align:right">Score</th>
          <th style="padding:6px 8px;text-align:left">Bias</th>
          <th style="padding:6px 8px;text-align:left">Conf</th>
          <th style="padding:6px 8px;text-align:right">Hits</th>
          <th style="padding:6px 8px;text-align:right">Srcs</th>
          <th style="padding:6px 8px;text-align:left">Catalyst</th>
          <th style="padding:6px 8px;text-align:left">Top Headline</th>
        </tr>
      </thead>
      <tbody>{rows_html}
      </tbody>
    </table>

    <p style="margin-top:20px;font-size:11px;color:#999">
      Generated by Pre-Market Catalyst Scanner &mdash; {scan_date}<br>
      Sources: Yahoo Finance, MarketWatch, Investing.com, Seeking Alpha,
      CNBC, PR Newswire, Finviz, SEC 8-K, and more.
    </p>
  </div>

</body>
</html>"""

    return plain, html


def send_email(ranked: list[tuple]) -> bool:
    """Send the scan results via SMTP. Returns True on success."""
    if not SMTP_USER or not SMTP_PASS:
        print(f"{Fore.YELLOW}[EMAIL] SMTP_USER / SMTP_PASS not set -- skipping email.{Style.RESET_ALL}")
        print(f"        Set env vars SMTP_USER and SMTP_PASS to enable.")
        return False

    now_str   = now_ct().strftime("%a %b %d %Y")
    # Runs after the 8:30 CT open (the 8:45 / 9:15 re-scans) are intraday
    # updates, not pre-market -- label them honestly.
    _t = now_ct()
    label = "Pre-Market" if (_t.hour, _t.minute) < (8, 30) else "Intraday"
    subject   = f"{label} Catalyst Scan  {_t.strftime('%I:%M %p CT')}  {now_str}  -- Top: " + \
                ", ".join(tk for tk, s, d in ranked[:5])

    plain, html = build_email_body(ranked)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html,  "html"))

    try:
        print(f"{Fore.CYAN}[EMAIL] Connecting to {SMTP_HOST}:{SMTP_PORT}...{Style.RESET_ALL}")
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())
        print(f"{Fore.GREEN}[EMAIL] Sent successfully to {EMAIL_TO}{Style.RESET_ALL}")
        return True
    except smtplib.SMTPAuthenticationError:
        print(f"{Fore.RED}[EMAIL] Auth failed -- check SMTP_USER and SMTP_PASS (use Gmail App Password){Style.RESET_ALL}")
    except Exception as e:
        print(f"{Fore.RED}[EMAIL] Failed: {e}{Style.RESET_ALL}")
    return False


# --- MAIN --------------------------------------------------------------------

def main():
    print(f"{Fore.YELLOW}{Style.BRIGHT}  PRE-MARKET CATALYST SCANNER  --  SCALP EDITION{Style.RESET_ALL}")
    print(f"  OVERNIGHT CATALYST SCANNER  --  SCALP EDITION{Style.RESET_ALL}")
    print()

    print(f"{Fore.CYAN}[1/3] Fetching overnight news from all sources...{Style.RESET_ALL}\n")
    items = run_all_scrapers()
    print(f"\n  {Fore.GREEN}Total items collected: {len(items)}{Style.RESET_ALL}\n")

    print(f"{Fore.CYAN}[2/3] Analyzing and scoring stocks...{Style.RESET_ALL}")
    ticker_data = aggregate_scores(items)
    print(f"  Unique tickers found: {len(ticker_data)}")

    print(f"\n{Fore.CYAN}[3/3] Ranking by move potential...{Style.RESET_ALL}\n")
    ranked = rank_tickers(ticker_data)
    apply_rules_flags(ranked)   # tag <$5 / <$500M rule violations (fails open)

    print_results(ranked)
    print_scalp_watchlist(ranked)

    # Email results
    send_email(ranked)

    # Save to file
    out_path = f"scan_{now_ct().strftime('%Y%m%d_%H%M')}.txt"
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(f"PRE-MARKET SCAN  {now_ct()}\n")
            f.write("="*80 + "\n\n")
            for rank, (tk, score, d) in enumerate(ranked, 1):
                bias = "BULL" if score > 0 else "BEAR"
                f.write(f"#{rank:2d}  {tk:<6}  {score:+.1f}  [{bias}]\n")
                f.write(f"     Catalyst: {', '.join(sorted(d['catalyst_type']))}\n")
                f.write(f"     Sources : {', '.join(sorted(d['sources']))[:70]}\n")
                for hl in d["headlines"][:2]:
                    f.write(f"     * {hl[:80]}\n")
                f.write("\n")
        print(f"{Fore.GREEN}Results saved to: {out_path}{Style.RESET_ALL}")
    except Exception as e:
        print(f"{Fore.RED}Could not save file: {e}{Style.RESET_ALL}")


if __name__ == "__main__":
    main()
