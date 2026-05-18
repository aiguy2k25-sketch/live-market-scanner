"""Macro Deployment Gate — Streamlit dashboard.

Entry point: refreshes data, recalculates all 6 signals + composite, renders
the dashboard. Dark theme (#0b0e17), deployment score as a huge number,
6 signal gauges, SPY chart color-coded by zone, performance comparison table.

Run with:
  streamlit run run_macro_gate.py
"""
from __future__ import annotations

import datetime as dt

import plotly.graph_objects as go
import streamlit as st

from backtest.deployment_backtest import run as run_backtest
from signals.composite import compute as compute_composite


# ---------------------------------------------------------------------------
# Page config + theme
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Macro Deployment Gate",
    page_icon="🚦",
    layout="wide",
    initial_sidebar_state="collapsed",
)

BG = "#0b0e17"
PANEL = "#141826"
TEXT = "#e5e7eb"
MUTED = "#9ca3af"
GREEN = "#22c55e"
YELLOW = "#eab308"
RED = "#ef4444"

st.markdown(f"""
<style>
  .stApp {{ background-color: {BG}; color: {TEXT}; }}
  section[data-testid="stSidebar"] {{ background-color: {PANEL}; }}
  div[data-testid="stMetricValue"] {{ color: {TEXT}; }}
  .big-score {{
    font-size: 140px;
    font-weight: 800;
    line-height: 1;
    text-align: center;
    font-variant-numeric: tabular-nums;
  }}
  .zone-label {{
    font-size: 32px;
    font-weight: 700;
    text-align: center;
    letter-spacing: 2px;
    margin-top: -10px;
  }}
  .zone-instruction {{
    font-size: 16px;
    color: {MUTED};
    text-align: center;
    margin-top: 4px;
  }}
  .panel {{
    background-color: {PANEL};
    border: 1px solid #1f2436;
    border-radius: 8px;
    padding: 16px;
    margin-bottom: 12px;
  }}
  .signal-name {{ color: {MUTED}; font-size: 13px; text-transform: uppercase;
                  letter-spacing: 1px; }}
  .signal-detail {{ color: {MUTED}; font-size: 12px; margin-top: 6px; }}
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Cached computations
# ---------------------------------------------------------------------------
@st.cache_data(ttl=900, show_spinner=False)   # 15 min
def cached_composite():
    return compute_composite()


@st.cache_data(ttl=3600, show_spinner=False)  # 1 hour
def cached_backtest():
    return run_backtest()


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------
def gauge(name: str, score: float, color: str, detail: str) -> go.Figure:
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=score,
        number={"font": {"color": TEXT, "size": 36}},
        gauge={
            "axis": {"range": [0, 100], "tickcolor": MUTED,
                     "tickfont": {"color": MUTED, "size": 10}},
            "bar": {"color": color, "thickness": 0.75},
            "bgcolor": PANEL,
            "borderwidth": 0,
            "steps": [
                {"range": [0, 40], "color": "#3b1f24"},
                {"range": [40, 70], "color": "#3b3322"},
                {"range": [70, 100], "color": "#1f3b27"},
            ],
        },
    ))
    fig.update_layout(
        height=180,
        margin=dict(l=10, r=10, t=10, b=10),
        paper_bgcolor=PANEL,
        font={"color": TEXT},
    )
    return fig


def zone_color_for_score(s: float) -> str:
    if s >= 70:
        return GREEN
    if s >= 40:
        return YELLOW
    return RED


def spy_chart_with_zones(daily) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=daily.index,
        y=daily["spy_close"],
        mode="lines",
        line=dict(color="#94a3b8", width=1.5),
        name="SPY",
        showlegend=False,
        hovertemplate="%{x|%Y-%m-%d}<br>SPY $%{y:.2f}<extra></extra>",
    ))

    # Color-coded zone bands using shaded rectangles
    zones = daily["zone"]
    colors = {"FULL DEPLOY": GREEN, "REDUCED": YELLOW, "DEFENSIVE": RED}
    # Find contiguous runs
    prev_zone = None
    run_start = None
    for d, z in zones.items():
        if z != prev_zone:
            if prev_zone is not None and run_start is not None:
                fig.add_vrect(
                    x0=run_start, x1=d,
                    fillcolor=colors.get(prev_zone, "#888"),
                    opacity=0.10, line_width=0, layer="below",
                )
            run_start = d
            prev_zone = z
    if prev_zone is not None and run_start is not None:
        fig.add_vrect(
            x0=run_start, x1=zones.index[-1],
            fillcolor=colors.get(prev_zone, "#888"),
            opacity=0.10, line_width=0, layer="below",
        )

    fig.update_layout(
        height=380,
        margin=dict(l=10, r=10, t=10, b=10),
        paper_bgcolor=PANEL,
        plot_bgcolor=PANEL,
        xaxis=dict(gridcolor="#1f2436", color=MUTED),
        yaxis=dict(gridcolor="#1f2436", color=MUTED, title="SPY"),
        hovermode="x",
    )
    return fig


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
col_title, col_refresh = st.columns([4, 1])
with col_title:
    st.markdown("## 🚦 Macro Deployment Gate")
    st.caption("Should I be deploying capital right now, and how aggressively?")
with col_refresh:
    st.write("")
    if st.button("🔄 Refresh data", use_container_width=True):
        cached_composite.clear()
        cached_backtest.clear()
        st.rerun()

st.markdown("---")

# ---------------------------------------------------------------------------
# Composite score block
# ---------------------------------------------------------------------------
with st.spinner("Loading live signals…"):
    try:
        comp = cached_composite()
    except Exception as e:
        st.error(f"Failed to compute composite: {e}")
        st.stop()

zone_color = comp.zone.color
score_str = f"{comp.score:.0f}"

st.markdown(
    f"<div class='big-score' style='color:{zone_color}'>{score_str}</div>"
    f"<div class='zone-label' style='color:{zone_color}'>{comp.zone.name}</div>"
    f"<div class='zone-instruction'>{comp.zone.instruction}</div>",
    unsafe_allow_html=True,
)
st.caption(f"Last refresh: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

if comp.errors:
    with st.expander(f"⚠️ {len(comp.errors)} signal(s) unavailable — weights renormalized"):
        for name, err in comp.errors.items():
            st.write(f"- **{name}**: {err}")

st.markdown("---")

# ---------------------------------------------------------------------------
# Six signal gauges
# ---------------------------------------------------------------------------
st.markdown("### Signal breakdown")
signal_order = ["VIX Level", "Term Structure", "Breadth",
                "Credit", "Put/Call", "Crowding"]
cols = st.columns(3)
for i, name in enumerate(signal_order):
    with cols[i % 3]:
        if name in comp.signals:
            r = comp.signals[name]
            color = zone_color_for_score(r.score)
            st.markdown(f"<div class='signal-name'>{name}</div>",
                        unsafe_allow_html=True)
            st.plotly_chart(gauge(name, r.score, color, r.detail),
                            use_container_width=True,
                            config={"displayModeBar": False})
            st.markdown(f"<div class='signal-detail'>{r.detail}</div>",
                        unsafe_allow_html=True)
        else:
            st.markdown(f"<div class='signal-name'>{name}</div>",
                        unsafe_allow_html=True)
            st.error("unavailable")

st.markdown("---")

# ---------------------------------------------------------------------------
# Historical backtest
# ---------------------------------------------------------------------------
st.markdown("### Historical backtest — trailing 2 years")

with st.spinner("Running backtest (one-time, ~30s)…"):
    try:
        bt = cached_backtest()
    except Exception as e:
        st.error(f"Backtest failed: {e}")
        bt = None

if bt is not None:
    st.plotly_chart(spy_chart_with_zones(bt.daily), use_container_width=True,
                    config={"displayModeBar": False})

    # Legend for the bands
    leg_c1, leg_c2, leg_c3 = st.columns(3)
    leg_c1.markdown(
        f"<span style='color:{GREEN}'>■</span> FULL DEPLOY",
        unsafe_allow_html=True)
    leg_c2.markdown(
        f"<span style='color:{YELLOW}'>■</span> REDUCED",
        unsafe_allow_html=True)
    leg_c3.markdown(
        f"<span style='color:{RED}'>■</span> DEFENSIVE",
        unsafe_allow_html=True)

    st.markdown("#### Average next-day SPY return by zone")
    bz = bt.by_zone.copy()
    bz_display = bz.style.format({
        "avg_fwd_ret_%": "{:+.3f}",
        "win_rate_%": "{:.1f}",
        "annualized_%": "{:+.1f}",
    }, na_rep="—")
    st.dataframe(bz_display, use_container_width=True)

    with st.expander("Backtest caveats"):
        for c in bt.caveats:
            st.write(f"- {c}")
