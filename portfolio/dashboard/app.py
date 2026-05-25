"""Streamlit dashboard: Overview + per-day decision flowcharts.

Run with:
    .venv/bin/streamlit run portfolio/dashboard/app.py
"""

from __future__ import annotations

import json
import os
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

from portfolio.state import store

load_dotenv()

st.set_page_config(
    page_title="AI Portfolio Manager — vs QQQ June 2026",
    layout="wide",
)


# ─── Data loaders ─────────────────────────────────────────────────────────────


@st.cache_data(ttl=30)
def load_nav(db_path: str) -> pd.DataFrame:
    conn = store.connect(db_path)
    df = pd.read_sql_query(
        "SELECT snapshot_date, portfolio_value, cash, equity, spy_close, qqq_close "
        "FROM nav_history ORDER BY snapshot_date",
        conn,
    )
    if not df.empty:
        df["snapshot_date"] = pd.to_datetime(df["snapshot_date"])
    return df


@st.cache_data(ttl=30)
def load_positions(db_path: str) -> pd.DataFrame:
    conn = store.connect(db_path)
    return pd.read_sql_query(
        "SELECT * FROM positions_snapshot "
        "WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM positions_snapshot) "
        "ORDER BY market_value DESC NULLS LAST",
        conn,
    )


@st.cache_data(ttl=30)
def load_decisions_summary(db_path: str, limit: int = 50) -> pd.DataFrame:
    conn = store.connect(db_path)
    return pd.read_sql_query(
        f"SELECT id, created_at, ticker, trade_date, action, "
        f"substr(reasoning, 1, 200) AS reasoning_preview "
        f"FROM agent_decisions ORDER BY id DESC LIMIT {int(limit)}",
        conn,
    )


@st.cache_data(ttl=30)
def load_trades(db_path: str, limit: int = 50) -> pd.DataFrame:
    conn = store.connect(db_path)
    return pd.read_sql_query(
        f"SELECT id, created_at, ticker, side, qty, status, "
        f"filled_qty, filled_avg_price, alpaca_order_id "
        f"FROM trades ORDER BY id DESC LIMIT {int(limit)}",
        conn,
    )


@st.cache_data(ttl=30)
def load_decisions_for_date(db_path: str, trade_date: str) -> pd.DataFrame:
    conn = store.connect(db_path)
    return pd.read_sql_query(
        "SELECT id, ticker, action, reasoning, raw_state "
        "FROM agent_decisions WHERE trade_date = ? ORDER BY ticker",
        conn,
        params=(trade_date,),
    )


@st.cache_data(ttl=30)
def list_decision_dates(db_path: str) -> list[str]:
    conn = store.connect(db_path)
    rows = conn.execute(
        "SELECT DISTINCT trade_date FROM agent_decisions ORDER BY trade_date DESC"
    ).fetchall()
    return [r[0] for r in rows]


# ─── Benchmark math ───────────────────────────────────────────────────────────


def compute_benchmark(nav: pd.DataFrame, starting_cash: float) -> pd.DataFrame:
    df = nav.copy()
    for col, out in (("qqq_close", "qqq_equivalent"), ("spy_close", "spy_equivalent")):
        if col in df.columns and df[col].dropna().any():
            first = df[col].dropna().iloc[0]
            df[out] = starting_cash * (df[col] / first)
        else:
            df[out] = None
    return df


# ─── Tab 1: Overview ──────────────────────────────────────────────────────────


def render_overview(db_path: str, starting_cash: float) -> None:
    nav = load_nav(db_path)
    nav = compute_benchmark(nav, starting_cash)

    col1, col2, col3, col4 = st.columns(4)
    if not nav.empty:
        latest_value = float(nav["portfolio_value"].iloc[-1])
        latest_qqq_eq = float(nav["qqq_equivalent"].iloc[-1]) if nav["qqq_equivalent"].notna().any() else None
        latest_spy_eq = float(nav["spy_equivalent"].iloc[-1]) if nav["spy_equivalent"].notna().any() else None
        col1.metric(
            "Portfolio NAV",
            f"${latest_value:,.0f}",
            f"{(latest_value - starting_cash) / starting_cash * 100:+.2f}%",
        )
        if latest_qqq_eq:
            qqq_pct = (latest_value - latest_qqq_eq) / latest_qqq_eq * 100
            col2.metric("vs QQQ", f"{qqq_pct:+.2f}%", f"${latest_value - latest_qqq_eq:+,.0f}")
        if latest_spy_eq:
            spy_pct = (latest_value - latest_spy_eq) / latest_spy_eq * 100
            col3.metric("vs SPY (context)", f"{spy_pct:+.2f}%")
        days_in_june = (date.today() - date(2026, 6, 1)).days
        col4.metric("Day of June run", f"{max(0, days_in_june)} / 30")
    else:
        col1.metric("Portfolio NAV", "—")

    st.subheader("NAV vs QQQ-equivalent (SPY for context)")
    if nav.empty:
        st.info("No NAV snapshots yet — the scheduler logs one per day.")
    else:
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=nav["snapshot_date"], y=nav["portfolio_value"],
            name="Portfolio", line=dict(width=3),
        ))
        if nav["qqq_equivalent"].notna().any():
            fig.add_trace(go.Scatter(
                x=nav["snapshot_date"], y=nav["qqq_equivalent"],
                name="QQQ-equivalent", line=dict(width=2, dash="dash"),
            ))
        if nav["spy_equivalent"].notna().any():
            fig.add_trace(go.Scatter(
                x=nav["snapshot_date"], y=nav["spy_equivalent"],
                name="SPY-equivalent", line=dict(width=1, dash="dot"), opacity=0.6,
            ))
        fig.update_layout(height=400, hovermode="x unified", yaxis_title="$ value", xaxis_title=None)
        st.plotly_chart(fig, use_container_width=True)

    st.subheader("Current positions")
    positions = load_positions(db_path)
    if positions.empty:
        st.info("No positions snapshotted yet.")
    else:
        st.dataframe(positions, use_container_width=True, hide_index=True)

    left, right = st.columns(2)
    with left:
        st.subheader("Recent agent decisions")
        st.dataframe(load_decisions_summary(db_path), use_container_width=True, hide_index=True)
    with right:
        st.subheader("Recent trades")
        st.dataframe(load_trades(db_path), use_container_width=True, hide_index=True)


# ─── Tab 2: Daily detail ──────────────────────────────────────────────────────


_RATING_COLOR = {
    "Buy":         "#2e7d32",  # green
    "Overweight":  "#7cb342",  # light green
    "Hold":        "#fdd835",  # yellow
    "Underweight": "#fb8c00",  # orange
    "Sell":        "#e53935",  # red
}


def _short(text: str | None, n: int = 90) -> str:
    if not text:
        return "(no output)"
    text = " ".join(text.split())
    return text[: n - 1] + "…" if len(text) > n else text


def build_flowchart_dot(ticker: str, state: dict, rating: str) -> str:
    """Build a graphviz DOT diagram of the agent flow for one ticker."""
    inv_debate = state.get("investment_debate_state") or {}
    risk_debate = state.get("risk_debate_state") or {}

    rating_color = _RATING_COLOR.get(rating, "#bdbdbd")

    def esc(s: str) -> str:
        return s.replace('"', "'").replace("\n", " ")

    nodes = []
    nodes.append(f'  ticker [label="{esc(ticker)}\\nTrade date", shape=oval, style=filled, fillcolor="#1976d2", fontcolor=white];')

    # Analysts (4)
    nodes.append(f'  market [label="Market Analyst\\n{esc(_short(state.get("market_report")))}", style=filled, fillcolor="#e3f2fd"];')
    nodes.append(f'  sentiment [label="Sentiment Analyst\\n{esc(_short(state.get("sentiment_report")))}", style=filled, fillcolor="#e3f2fd"];')
    nodes.append(f'  news [label="News Analyst\\n{esc(_short(state.get("news_report")))}", style=filled, fillcolor="#e3f2fd"];')
    nodes.append(f'  fundamentals [label="Fundamentals Analyst\\n{esc(_short(state.get("fundamentals_report")))}", style=filled, fillcolor="#e3f2fd"];')

    # Researchers (bull/bear)
    nodes.append(f'  bull [label="Bull Researcher\\n{esc(_short(inv_debate.get("current_response") or inv_debate.get("history")))}", style=filled, fillcolor="#fff8e1"];')
    nodes.append(f'  bear [label="Bear Researcher\\n{esc(_short(inv_debate.get("bear_history")))}", style=filled, fillcolor="#fff8e1"];')

    # Research Manager / Investment Plan
    nodes.append(f'  research_mgr [label="Research Manager\\n{esc(_short(state.get("investment_plan")))}", style=filled, fillcolor="#fff3e0"];')

    # Trader
    nodes.append(f'  trader [label="Trader\\n{esc(_short(state.get("trader_investment_plan")))}", style=filled, fillcolor="#e8f5e9"];')

    # Risk team (3)
    nodes.append(f'  risky [label="Risky Analyst\\n{esc(_short(risk_debate.get("current_aggressive_response")))}", style=filled, fillcolor="#ffebee"];')
    nodes.append(f'  neutral [label="Neutral Analyst\\n{esc(_short(risk_debate.get("current_neutral_response")))}", style=filled, fillcolor="#ffebee"];')
    nodes.append(f'  safe [label="Safe Analyst\\n{esc(_short(risk_debate.get("current_conservative_response")))}", style=filled, fillcolor="#ffebee"];')

    # PM (final)
    nodes.append(
        f'  pm [label="Portfolio Manager\\nRATING: {esc(rating).upper()}", '
        f'shape=box, style="filled,bold", fillcolor="{rating_color}", fontcolor=white, fontsize=14];'
    )

    edges = """
      ticker -> market;
      ticker -> sentiment;
      ticker -> news;
      ticker -> fundamentals;
      market -> bull;
      market -> bear;
      sentiment -> bull;
      sentiment -> bear;
      news -> bull;
      news -> bear;
      fundamentals -> bull;
      fundamentals -> bear;
      bull -> research_mgr;
      bear -> research_mgr;
      research_mgr -> trader;
      trader -> risky;
      trader -> neutral;
      trader -> safe;
      risky -> pm;
      neutral -> pm;
      safe -> pm;
    """

    body = "\n".join(nodes) + edges
    return f'digraph G {{\n  rankdir=TB;\n  node [shape=box, fontname="Helvetica", fontsize=10];\n  edge [color="#999999"];\n{body}\n}}'


def render_decision_card(row: pd.Series) -> None:
    state = {}
    if row.get("raw_state"):
        try:
            state = json.loads(row["raw_state"])
        except Exception:
            state = {}

    rating = (row["action"] or "Hold").title()
    color = _RATING_COLOR.get(rating, "#bdbdbd")

    st.markdown(
        f"### {row['ticker']} — "
        f"<span style='color:{color}; font-weight:700'>{rating.upper()}</span>",
        unsafe_allow_html=True,
    )

    dot = build_flowchart_dot(row["ticker"], state, rating)
    st.graphviz_chart(dot, use_container_width=True)

    with st.expander("Full agent reports"):
        sections = [
            ("Market Analyst", state.get("market_report")),
            ("Sentiment Analyst", state.get("sentiment_report")),
            ("News Analyst", state.get("news_report")),
            ("Fundamentals Analyst", state.get("fundamentals_report")),
            ("Research Manager (Investment Plan)", state.get("investment_plan")),
            ("Trader (Transaction Proposal)", state.get("trader_investment_plan")),
            ("Portfolio Manager (Final Decision)", state.get("final_trade_decision")),
        ]
        for title, content in sections:
            if content:
                st.markdown(f"**{title}**")
                st.markdown(str(content))
                st.divider()


def render_daily_detail(db_path: str, starting_cash: float) -> None:
    nav = load_nav(db_path)
    dates = list_decision_dates(db_path)

    if not dates:
        st.info("No decisions logged yet. Run `python -m portfolio.run_live <TICKER>` to populate.")
        return

    selected = st.selectbox("Pick a day", dates, index=0)

    # Daily metrics: day number, portfolio return today, QQQ return today, NAV
    nav_indexed = nav.set_index(nav["snapshot_date"].dt.strftime("%Y-%m-%d")) if not nav.empty else pd.DataFrame()
    today_row = nav_indexed.loc[selected] if selected in nav_indexed.index else None

    col1, col2, col3, col4 = st.columns(4)
    if today_row is not None:
        days_in = (date.fromisoformat(selected) - date(2026, 6, 1)).days + 1
        col1.metric("Day", f"{max(1, days_in)} of 30")
        col2.metric("Portfolio value", f"${float(today_row['portfolio_value']):,.0f}")

        # Daily returns from previous trading day
        prior = nav_indexed.iloc[nav_indexed.index.get_loc(selected) - 1] if nav_indexed.index.get_loc(selected) > 0 else None
        if prior is not None:
            port_ret = (today_row["portfolio_value"] - prior["portfolio_value"]) / prior["portfolio_value"] * 100
            col3.metric("Portfolio return (today)", f"{port_ret:+.2f}%")
            if today_row.get("qqq_close") and prior.get("qqq_close"):
                qqq_ret = (today_row["qqq_close"] - prior["qqq_close"]) / prior["qqq_close"] * 100
                col4.metric("QQQ return (today)", f"{qqq_ret:+.2f}%")
    else:
        col1.metric("Day", "—")
        col2.info("No NAV snapshot for this date yet.")

    st.divider()

    # Per-ticker decisions for this day, each as a flowchart card.
    decisions = load_decisions_for_date(db_path, selected)
    if decisions.empty:
        st.info(f"No decisions on {selected}.")
        return

    st.markdown(f"## {len(decisions)} decision(s) on {selected}")
    for _, row in decisions.iterrows():
        render_decision_card(row)
        st.divider()


# ─── Tab 3: Memory & Learning ─────────────────────────────────────────────────


def _default_memory_path() -> str:
    return os.environ.get(
        "TRADINGAGENTS_MEMORY_LOG_PATH",
        os.path.expanduser("~/.tradingagents/memory/trading_memory.md"),
    )


def _parse_memory_log(path: str) -> list[dict]:
    """Parse upstream's markdown memory log into per-entry dicts.

    Each entry starts with a header like:
      [2024-05-10 | NVDA | Overweight | +2.9% | +1.2% | 5d]
    or, for unresolved entries:
      [2024-05-10 | NVDA | Overweight | pending]
    """
    if not os.path.exists(path):
        return []
    raw = open(path).read()
    chunks = [c.strip() for c in raw.split("<!-- ENTRY_END -->") if c.strip()]
    out = []
    for chunk in chunks:
        lines = chunk.split("\n", 1)
        if not lines or not lines[0].startswith("["):
            continue
        header = lines[0].strip("[]").strip()
        parts = [p.strip() for p in header.split("|")]
        entry: dict = {
            "date": parts[0] if len(parts) > 0 else None,
            "ticker": parts[1] if len(parts) > 1 else None,
            "rating": parts[2] if len(parts) > 2 else None,
            "raw_return": parts[3] if len(parts) > 3 else None,
            "alpha": parts[4] if len(parts) > 4 else None,
            "holding": parts[5] if len(parts) > 5 else None,
            "status": "pending" if "pending" in header.lower() else "resolved",
            "body": lines[1] if len(lines) > 1 else "",
        }
        out.append(entry)
    return out


def render_memory(starting_cash: float) -> None:
    path = _default_memory_path()
    st.caption(f"Source: {path}")

    entries = _parse_memory_log(path)
    if not entries:
        st.info(
            "Memory log is empty. It will populate automatically as the agent "
            "makes decisions — each new run pulls in past lessons from prior "
            "same-ticker decisions and writes a reflection once price data is "
            "available."
        )
        return

    resolved = [e for e in entries if e["status"] == "resolved"]
    pending = [e for e in entries if e["status"] == "pending"]

    col1, col2, col3 = st.columns(3)
    col1.metric("Total memories", len(entries))
    col2.metric("Resolved (have outcome)", len(resolved))
    col3.metric("Pending (awaiting price data)", len(pending))

    st.markdown(
        "These are the lessons fed to the Portfolio Manager before each new "
        "decision. Alpha is measured against **QQQ** (our benchmark)."
    )

    # Resolved entries first (with reflection)
    st.subheader("Resolved decisions — what the agent learned")
    if not resolved:
        st.info("No resolved entries yet. Returns become available ~5 trading days after a decision.")
    for e in reversed(resolved):  # newest first
        with st.expander(
            f"{e['date']} · {e['ticker']} · {e['rating']} · "
            f"alpha {e['alpha']} (raw {e['raw_return']}, held {e['holding']})"
        ):
            st.markdown(e["body"])

    if pending:
        st.subheader("Pending decisions — waiting for outcome")
        for e in reversed(pending):
            with st.expander(f"{e['date']} · {e['ticker']} · {e['rating']} (pending)"):
                st.markdown(e["body"])


# ─── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    db_path = os.environ.get("PORTFOLIO_DB_PATH", "./portfolio_state.db")
    starting_cash = float(os.environ.get("PORTFOLIO_STARTING_CASH", "100000"))

    st.title("AI Portfolio Manager")
    st.caption("Live paper-trading vs QQQ for June 2026 (SPY shown for context).")

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

    tab1, tab2, tab3 = st.tabs(
        ["Overview", "Daily detail (for reels)", "Memory & Learning"]
    )
    with tab1:
        render_overview(db_path, starting_cash)
    with tab2:
        render_daily_detail(db_path, starting_cash)
    with tab3:
        render_memory(starting_cash)


if __name__ == "__main__":
    main()
