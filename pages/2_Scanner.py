"""Page 2 — Quantitative Scanner.

Renders inside the existing Streamlit multipage app. Reads the macro gate,
runs the 5-factor scan, and displays the ranked table.

Streamlit auto-discovers files in /pages and adds them to the sidebar nav.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st

from scanner import run as run_scanner
from scanner.config import TOP_N_DISPLAY


# Theme constants (match run_macro_gate.py)
BG = "#0b0e17"
PANEL = "#141826"
TEXT = "#e5e7eb"
MUTED = "#9ca3af"
GREEN = "#22c55e"
YELLOW = "#eab308"
RED = "#ef4444"

st.set_page_config(
    page_title="Scanner — Macro Gate",
    page_icon="🔎",
    layout="wide",
)

st.markdown(f"""
<style>
  .stApp {{ background-color: {BG}; color: {TEXT}; }}
  section[data-testid="stSidebar"] {{ background-color: {PANEL}; }}
  .zone-pill {{
    display: inline-block;
    padding: 4px 12px;
    border-radius: 999px;
    font-weight: 700;
    font-size: 14px;
    letter-spacing: 1px;
  }}
</style>
""", unsafe_allow_html=True)


@st.cache_data(ttl=900, show_spinner=False)
def cached_scan():
    return run_scanner()


def zone_color(zone: str) -> str:
    return {"FULL DEPLOY": GREEN, "REDUCED": YELLOW,
            "DEFENSIVE": RED}.get(zone, MUTED)


st.markdown("## 🔎 Quantitative Scanner")
st.caption("5 factors · S&P 500 universe · macro-gated activation")

col_status, col_refresh = st.columns([4, 1])
with col_refresh:
    if st.button("🔄 Rescan", use_container_width=True):
        cached_scan.clear()
        st.rerun()

with st.spinner("Running scan (~30–60s on first load)…"):
    try:
        result = cached_scan()
    except Exception as e:
        st.error(f"Scanner failed: {e}")
        st.stop()

with col_status:
    color = zone_color(result.zone)
    st.markdown(
        f"<span class='zone-pill' style='background:{color}22; "
        f"color:{color}; border:1px solid {color}'>{result.zone}</span> "
        f"&nbsp; macro composite **{result.score:.1f}**",
        unsafe_allow_html=True,
    )

for n in result.notes:
    st.write(f"• {n}")

st.markdown("---")

if result.disabled:
    st.markdown(
        f"<div style='padding:40px; text-align:center; "
        f"background:{PANEL}; border-radius:8px; color:{MUTED}'>"
        f"<div style='font-size:48px'>⛔</div>"
        f"<div style='font-size:18px; font-weight:600; color:{RED}; "
        f"margin-top:8px'>SCANNER DISABLED</div>"
        f"<div style='margin-top:8px'>Defensive zone — no new longs.</div>"
        f"</div>",
        unsafe_allow_html=True,
    )
    st.stop()

# --- Top N table ----------------------------------------------------------
df = result.df.head(TOP_N_DISPLAY).copy()
if df.empty:
    st.warning("No stocks meet the current thresholds.")
    st.stop()

display_cols = {
    "price": "Price",
    "composite": "Composite",
    "score_1_momentum": "Momentum",
    "score_2_vol_surge": "Vol Surge",
    "score_3_rs": "RS",
    "score_4_high_prox": "52w Hi",
    "score_5_short": "Short",
    "f4_pct_of_52w_high": "% of 52w Hi",
    "f2_vol_surge_ratio": "Vol Ratio",
    "f3_rs_vs_spy": "RS Δ %",
    "f5_short_pct_float": "Short % Float",
}
shown = df[list(display_cols.keys())].rename(columns=display_cols)

styler = (
    shown.style
    .format({
        "Price": "${:.2f}",
        "Composite": "{:.1f}",
        "Momentum": "{:.0f}",
        "Vol Surge": "{:.0f}",
        "RS": "{:.0f}",
        "52w Hi": "{:.0f}",
        "Short": "{:.0f}",
        "% of 52w Hi": "{:.1%}",
        "Vol Ratio": "{:.2f}",
        "RS Δ %": "{:+.1f}",
        "Short % Float": "{:.1%}",
    }, na_rep="—")
    .background_gradient(subset=["Composite"], cmap="RdYlGn", vmin=0, vmax=100)
)

st.markdown(f"### Top {len(shown)} of {result.universe_size}")
st.dataframe(styler, use_container_width=True, height=600)

# --- Coverage report ------------------------------------------------------
with st.expander("Factor data coverage"):
    cov_df = pd.DataFrame(
        [{"Factor": k, "Tickers with data": v,
          "Coverage %": f"{100*v/result.universe_size:.0f}%"}
         for k, v in result.coverage.items()]
    )
    st.dataframe(cov_df, use_container_width=True, hide_index=True)

# --- Download -------------------------------------------------------------
csv = result.df.to_csv().encode()
st.download_button(
    label=f"📥  Download full ranked CSV ({len(result.df)} rows)",
    data=csv,
    file_name=f"scan_{dt.date.today():%Y%m%d}.csv",
    mime="text/csv",
)

# --- Caveats --------------------------------------------------------------
with st.expander("Methodology & caveats"):
    st.markdown("""
- **Each factor is percentile-ranked across the universe** (0–100).
  Composite is the equal-weighted mean of the 5 factor scores.
- **Factor 1 (Momentum X-over)**: requires a 10-EMA over 50-EMA crossover
  within the last 5 days; non-crossover names score at the floor.
- **Factor 5 (Short Interest)**: implemented as a *level* proxy using current
  `shortPercentOfFloat` from Yahoo Finance — lower is better. The spec asks
  for *change* over time, which requires a paid feed (FINRA / Sharadar);
  the live signal is otherwise faithful.
- Names with insufficient data on a given factor get a floor rank for that
  factor only; their composite penalizes them appropriately.
    """)
