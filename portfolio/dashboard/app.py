"""Streamlit dashboard: NAV vs SPY, positions, decisions, trades.

Run with:
    .venv/bin/streamlit run portfolio/dashboard/app.py
"""

from __future__ import annotations

import os
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

from portfolio.state import store

load_dotenv()

st.set_page_config(
    page_title="AI Portfolio Manager — vs SPY June 2026",
    layout="wide",
)


@st.cache_data(ttl=30)
def load_nav(db_path: str) -> pd.DataFrame:
    conn = store.connect(db_path)
    df = pd.read_sql_query(
        "SELECT snapshot_date, portfolio_value, cash, equity, spy_close "
        "FROM nav_history ORDER BY snapshot_date",
        conn,
    )
    if not df.empty:
        df["snapshot_date"] = pd.to_datetime(df["snapshot_date"])
    return df


@st.cache_data(ttl=30)
def load_positions(db_path: str) -> pd.DataFrame:
    conn = store.connect(db_path)
    df = pd.read_sql_query(
        "SELECT * FROM positions_snapshot "
        "WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM positions_snapshot) "
        "ORDER BY market_value DESC NULLS LAST",
        conn,
    )
    return df


@st.cache_data(ttl=30)
def load_decisions(db_path: str, limit: int = 50) -> pd.DataFrame:
    conn = store.connect(db_path)
    df = pd.read_sql_query(
        f"SELECT id, created_at, ticker, trade_date, action, "
        f"substr(reasoning, 1, 200) AS reasoning_preview "
        f"FROM agent_decisions ORDER BY id DESC LIMIT {int(limit)}",
        conn,
    )
    return df


@st.cache_data(ttl=30)
def load_trades(db_path: str, limit: int = 50) -> pd.DataFrame:
    conn = store.connect(db_path)
    df = pd.read_sql_query(
        f"SELECT id, created_at, ticker, side, qty, status, "
        f"filled_qty, filled_avg_price, alpaca_order_id "
        f"FROM trades ORDER BY id DESC LIMIT {int(limit)}",
        conn,
    )
    return df


def compute_benchmark(nav: pd.DataFrame, starting_cash: float) -> pd.DataFrame:
    """Compute 'what if we held SPY from day 1' equivalent NAV."""
    if nav.empty or nav["spy_close"].dropna().empty:
        return nav.assign(spy_equivalent=None)
    first_spy = nav["spy_close"].dropna().iloc[0]
    spy_equivalent = starting_cash * (nav["spy_close"] / first_spy)
    return nav.assign(spy_equivalent=spy_equivalent)


def main() -> None:
    db_path = os.environ.get("PORTFOLIO_DB_PATH", "./portfolio_state.db")
    starting_cash = float(os.environ.get("PORTFOLIO_STARTING_CASH", "100000"))

    st.title("AI Portfolio Manager")
    st.caption("Live paper-trading vs SPY for June 2026.")

    with st.sidebar:
        st.header("Config")
        st.text(f"DB: {db_path}")
        st.text(f"Start cash: ${starting_cash:,.0f}")
        if st.button("Refresh data"):
            st.cache_data.clear()
            st.rerun()

    if not os.path.exists(db_path):
        st.warning(f"No DB at {db_path} yet. Run `python -m portfolio.run_live <TICKER>` first.")
        st.stop()

    nav = load_nav(db_path)
    nav = compute_benchmark(nav, starting_cash)

    # Headline metrics
    col1, col2, col3, col4 = st.columns(4)
    if not nav.empty:
        latest_value = float(nav["portfolio_value"].iloc[-1])
        latest_spy_eq = float(nav["spy_equivalent"].iloc[-1]) if nav["spy_equivalent"].notna().any() else None
        col1.metric("Portfolio NAV", f"${latest_value:,.0f}",
                    f"{(latest_value - starting_cash) / starting_cash * 100:+.2f}%")
        if latest_spy_eq:
            delta = latest_value - latest_spy_eq
            col2.metric("vs SPY ($)", f"${delta:+,.0f}")
            col3.metric("vs SPY (%)",
                        f"{(latest_value - latest_spy_eq) / latest_spy_eq * 100:+.2f}%")
        days_in_june = (date.today() - date(2026, 6, 1)).days
        col4.metric("Day of June run", f"{max(0, days_in_june)} / 30")
    else:
        col1.metric("Portfolio NAV", "—")

    # NAV curve
    st.subheader("NAV vs SPY-equivalent")
    if nav.empty:
        st.info("No NAV snapshots yet — the scheduler logs one per day.")
    else:
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=nav["snapshot_date"], y=nav["portfolio_value"],
            name="Portfolio", line=dict(width=3),
        ))
        if nav["spy_equivalent"].notna().any():
            fig.add_trace(go.Scatter(
                x=nav["snapshot_date"], y=nav["spy_equivalent"],
                name="SPY-equivalent", line=dict(dash="dash"),
            ))
        fig.update_layout(
            height=400, hovermode="x unified",
            yaxis_title="$ value", xaxis_title=None,
        )
        st.plotly_chart(fig, use_container_width=True)

    # Positions
    st.subheader("Current positions")
    positions = load_positions(db_path)
    if positions.empty:
        st.info("No positions snapshotted yet.")
    else:
        st.dataframe(positions, use_container_width=True, hide_index=True)

    # Decisions + trades side by side
    left, right = st.columns(2)
    with left:
        st.subheader("Recent agent decisions")
        st.dataframe(load_decisions(db_path), use_container_width=True, hide_index=True)
    with right:
        st.subheader("Recent trades")
        st.dataframe(load_trades(db_path), use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
