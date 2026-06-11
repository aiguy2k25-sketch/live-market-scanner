"""
Ticker Universe — validates extracted tickers against the real listed universe.

Primary source: SEC company_tickers.json (~10,000 US-listed companies,
ticker + company name, updated daily by the SEC, no API key needed).

Provides:
  - is_valid_ticker(t)       -> bool
  - company_name_for(t)      -> str | None
  - tickers_from_company_names(text) -> set[str]   (for 8-K filings etc.)

Falls back to a built-in core set if the SEC download fails, so the
scanner never hard-crashes in CI.
"""

import json
import os
import re
import time

import requests

SEC_URL = "https://www.sec.gov/files/company_tickers.json"
CACHE_FILE = os.path.join(os.path.dirname(__file__), ".ticker_universe_cache.json")
CACHE_TTL_SECONDS = 7 * 24 * 3600  # refresh weekly

# SEC requires a descriptive User-Agent
SEC_HEADERS = {"User-Agent": "premarket-scanner personal-research 2daysale@gmail.com"}

# Minimal fallback so the scanner still works if sec.gov is unreachable
_FALLBACK = {
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "NVDA", "AMD", "INTC",
    "QCOM", "AVGO", "ARM", "MU", "MRVL", "CRDO", "KLAC", "AMAT", "LRCX", "TSM",
    "JPM", "BAC", "WFC", "GS", "MS", "C", "USB", "SCHW", "BLK", "HOOD", "SOFI",
    "XOM", "CVX", "COP", "OXY", "SLB", "HAL", "DVN", "MPC", "VLO", "PSX",
    "CRK", "SM", "PBF", "PARR", "MUSA", "FANG", "EOG", "APA",
    "JNJ", "PFE", "ABBV", "MRK", "BMY", "LLY", "AMGN", "GILD", "REGN", "VRTX",
    "MRNA", "HUM", "CNC", "UNH", "CVS", "OSCR", "ALHC", "TGTX", "ILMN",
    "HD", "WMT", "COST", "TGT", "LOW", "DG", "DLTR", "KR", "CASY", "CBRL",
    "BA", "LMT", "RTX", "NOC", "GD", "LDOS", "SAIC", "KTOS", "RKLB", "RDW",
    "T", "VZ", "TMUS", "CMCSA", "CHTR", "TLK",
    "NFLX", "DIS", "WBD", "PARA", "SPOT", "SNAP", "PINS", "RBLX",
    "CRM", "NOW", "ORCL", "SAP", "INTU", "ADBE", "WDAY", "DDOG", "MDB", "PATH",
    "PYPL", "V", "MA", "AXP", "COF", "AFRM", "UPST",
    "SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "TLT",
    "COIN", "MSTR", "MARA", "RIOT", "CLSK", "IREN",
    "NIO", "RIVN", "LCID", "F", "GM", "TM",
    "PLTR", "SMCI", "CRWD", "PANW", "FTNT", "ZS", "CYBR",
    "VELO", "BKH", "NWE", "AAOI", "AXTI", "CAVA", "PENN", "UNFI", "MAX",
    "LAKE", "NTES", "PEB", "EYE", "GTE", "NEOG", "NI", "ARTV", "HCAI",
}

_universe: dict[str, str] = {}   # ticker -> company name
_name_index: list[tuple[str, str]] = []  # (lowercase company name, ticker)
_loaded = False


def _load() -> None:
    global _universe, _name_index, _loaded
    if _loaded:
        return

    data = None

    # 1) try cache
    try:
        if os.path.exists(CACHE_FILE):
            age = time.time() - os.path.getmtime(CACHE_FILE)
            if age < CACHE_TTL_SECONDS:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
    except Exception:
        data = None

    # 2) try SEC download
    if data is None:
        try:
            r = requests.get(SEC_URL, headers=SEC_HEADERS, timeout=15)
            r.raise_for_status()
            data = r.json()
            try:
                with open(CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f)
            except Exception:
                pass
        except Exception as e:
            print(f"  [UNIVERSE] SEC download failed ({e}); using fallback set "
                  f"({len(_FALLBACK)} tickers)")
            data = None

    if data:
        # SEC format: {"0": {"cik_str":..., "ticker":"AAPL", "title":"Apple Inc."}, ...}
        for row in data.values():
            tk = str(row.get("ticker", "")).upper().strip()
            title = str(row.get("title", "")).strip()
            if tk:
                _universe[tk] = title
        print(f"  [UNIVERSE] Loaded {len(_universe)} tickers from SEC")
    else:
        _universe = {t: "" for t in _FALLBACK}

    # Build a company-name index for name->ticker matching (8-K filings).
    # Strip common suffixes so "Apple Inc." matches "Apple".
    suffix_re = re.compile(
        r"\b(incorporated|corporation|company|holdings?|group|inc|corp|co|ltd|plc|"
        r"lp|llc|sa|nv|ag|adr|cl[ ]?[ab])\b\.?", re.IGNORECASE)
    for tk, title in _universe.items():
        if not title:
            continue
        clean = suffix_re.sub("", title).strip(" ,.&/").lower()
        # require a reasonably distinctive name (avoid matching "AT" or "ON")
        if len(clean) >= 5:
            _name_index.append((clean, tk))
    # longest names first so "Cracker Barrel Old Country Store" wins over "Cracker"
    _name_index.sort(key=lambda x: -len(x[0]))
    _loaded = True


def is_valid_ticker(t: str) -> bool:
    _load()
    return t.upper() in _universe


def company_name_for(t: str) -> str | None:
    _load()
    return _universe.get(t.upper()) or None


def tickers_from_company_names(text: str, max_hits: int = 3) -> set[str]:
    """Find tickers by matching company names inside text (e.g., SEC 8-K titles)."""
    _load()
    found: set[str] = set()
    lower = text.lower()
    for name, tk in _name_index:
        if len(found) >= max_hits:
            break
        if name in lower:
            found.add(tk)
    return found


def universe_size() -> int:
    _load()
    return len(_universe)
