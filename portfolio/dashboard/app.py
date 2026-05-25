"""Streamlit dashboard — light theme, T-N countdown, side-by-side vs QQQ, architecture view.

Run locally:
    .venv/bin/streamlit run portfolio/dashboard/app.py
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

from portfolio.state import store

load_dotenv()

LAUNCH_DATE = date(2026, 6, 1)
END_DATE = date(2026, 6, 30)
TOTAL_DAYS = 30

st.set_page_config(
    page_title="AI Portfolio Manager — vs QQQ",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Custom CSS for a clean, light, "dashboard" feel.
st.markdown(
    """
    <style>
    .day-banner {
        font-size: 5rem;
        font-weight: 800;
        line-height: 1;
        margin: 0.25rem 0 0.5rem 0;
        color: #111827;
        letter-spacing: -0.02em;
    }
    .day-sub {
        color: #6b7280;
        font-size: 1rem;
        margin-top: -0.5rem;
        margin-bottom: 1.5rem;
    }
    .metric-card {
        background: #ffffff;
        border: 1px solid #e5e7eb;
        border-radius: 12px;
        padding: 1.5rem;
        height: 100%;
    }
    .metric-label {
        color: #6b7280;
        font-size: 0.875rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin-bottom: 0.5rem;
    }
    .metric-value {
        font-size: 2.75rem;
        font-weight: 700;
        color: #111827;
        line-height: 1.1;
    }
    .metric-delta {
        font-size: 1rem;
        font-weight: 600;
        margin-top: 0.4rem;
    }
    .delta-pos { color: #059669; }
    .delta-neg { color: #dc2626; }
    .delta-neutral { color: #6b7280; }
    </style>
    """,
    unsafe_allow_html=True,
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
def load_decisions_summary(db_path: str) -> pd.DataFrame:
    conn = store.connect(db_path)
    return pd.read_sql_query(
        "SELECT created_at, ticker, trade_date, action FROM agent_decisions "
        "ORDER BY id DESC",
        conn,
    )


@st.cache_data(ttl=30)
def load_trades(db_path: str) -> pd.DataFrame:
    conn = store.connect(db_path)
    return pd.read_sql_query(
        "SELECT created_at, ticker, side, qty, status, filled_qty, filled_avg_price "
        "FROM trades ORDER BY id DESC LIMIT 50",
        conn,
    )


# ─── Header: T-N countdown / Day-of-30 ────────────────────────────────────────


def trading_days_between(start_exclusive: date, end_exclusive: date) -> int:
    days = 0
    d = start_exclusive + timedelta(days=1)
    while d < end_exclusive:
        if d.weekday() < 5:  # Mon–Fri
            days += 1
        d += timedelta(days=1)
    return days


def day_label() -> tuple[str, str]:
    """Counter: trading days from tomorrow (inclusive) until launch (exclusive).

    On Mon May 25 (Memorial Day): Tue 26, Wed 27, Thu 28, Fri 29 → T-4.
    On Tue May 26 (post-run):     Wed 27, Thu 28, Fri 29           → T-3.
    """
    today = date.today()
    if today < LAUNCH_DATE:
        n = trading_days_between(today, LAUNCH_DATE)
        return f"Day: T-{n}", f"{n} trading day{'s' if n != 1 else ''} until June 1 launch"
    if today > END_DATE:
        return "Day: Final", "Run complete — see Memory & Learning for post-mortem"
    day_num = (today - LAUNCH_DATE).days + 1
    return f"Day: {day_num} of {TOTAL_DAYS}", f"{LAUNCH_DATE} → {END_DATE}"


def compute_benchmark(nav: pd.DataFrame, starting_cash: float) -> pd.DataFrame:
    df = nav.copy()
    for col, out in (("qqq_close", "qqq_equivalent"), ("spy_close", "spy_equivalent")):
        if col in df.columns and df[col].dropna().any():
            first = df[col].dropna().iloc[0]
            df[out] = starting_cash * (df[col] / first)
        else:
            df[out] = None
    return df


def fmt_delta(value: float, kind: str = "pct") -> str:
    if value is None:
        return "<span class='metric-delta delta-neutral'>—</span>"
    cls = "delta-pos" if value > 0 else ("delta-neg" if value < 0 else "delta-neutral")
    sign = "+" if value > 0 else ""
    suffix = "%" if kind == "pct" else ""
    return f"<span class='metric-delta {cls}'>{sign}{value:.2f}{suffix}</span>"


def render_metric_card(label: str, value: str, delta_html: str) -> None:
    st.markdown(
        f"""
        <div class="metric-card">
          <div class="metric-label">{label}</div>
          <div class="metric-value">{value}</div>
          {delta_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


# ─── Tab 1: Overview ──────────────────────────────────────────────────────────


def render_overview(db_path: str, starting_cash: float) -> None:
    nav = load_nav(db_path)
    nav = compute_benchmark(nav, starting_cash)

    has_data = not nav.empty

    if has_data:
        latest_portfolio = float(nav["portfolio_value"].iloc[-1])
        portfolio_total_return = (latest_portfolio - starting_cash) / starting_cash * 100

        latest_qqq_eq = (
            float(nav["qqq_equivalent"].iloc[-1])
            if nav["qqq_equivalent"].notna().any() else None
        )
        qqq_total_return = (
            (latest_qqq_eq - starting_cash) / starting_cash * 100
            if latest_qqq_eq else None
        )
        alpha = (
            (latest_portfolio - latest_qqq_eq) / latest_qqq_eq * 100
            if latest_qqq_eq else None
        )
    else:
        latest_portfolio = starting_cash
        portfolio_total_return = 0.0
        latest_qqq_eq = starting_cash
        qqq_total_return = 0.0
        alpha = 0.0

    # Side-by-side Portfolio vs QQQ comparison.
    col_p, col_q, col_a = st.columns([1, 1, 1])
    with col_p:
        render_metric_card(
            "Portfolio",
            f"${latest_portfolio:,.0f}",
            fmt_delta(portfolio_total_return),
        )
    with col_q:
        render_metric_card(
            "QQQ Benchmark",
            f"${latest_qqq_eq:,.0f}" if latest_qqq_eq else "—",
            fmt_delta(qqq_total_return),
        )
    with col_a:
        render_metric_card(
            "Alpha (vs QQQ)",
            f"{alpha:+.2f}%" if alpha is not None else "—",
            f"<span class='metric-delta {'delta-pos' if alpha and alpha > 0 else 'delta-neg' if alpha and alpha < 0 else 'delta-neutral'}'>"
            f"${latest_portfolio - latest_qqq_eq:+,.0f}</span>"
            if latest_qqq_eq else "<span class='metric-delta delta-neutral'>—</span>",
        )

    st.markdown("<br>", unsafe_allow_html=True)

    if not has_data:
        st.info(
            "📊 No portfolio data yet. The first scheduled run is **June 1, 2026 at 16:00 ET**. "
            "Charts will appear here after that."
        )

    # NAV curve
    st.subheader("Portfolio value over time")
    if has_data:
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=nav["snapshot_date"], y=nav["portfolio_value"],
            name="Portfolio", line=dict(color="#1976d2", width=3),
            mode="lines+markers",
        ))
        if nav["qqq_equivalent"].notna().any():
            fig.add_trace(go.Scatter(
                x=nav["snapshot_date"], y=nav["qqq_equivalent"],
                name="QQQ-equivalent", line=dict(color="#9ca3af", width=2, dash="dash"),
                mode="lines",
            ))
        fig.update_layout(
            height=380, hovermode="x unified",
            yaxis_title="$ value", xaxis_title=None,
            paper_bgcolor="white", plot_bgcolor="white",
            yaxis=dict(gridcolor="#e5e7eb"), xaxis=dict(gridcolor="#e5e7eb"),
            margin=dict(t=20, b=20, l=20, r=20),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.empty()

    # Daily returns bar chart + position allocation pie
    if has_data and len(nav) >= 2:
        nav_sorted = nav.sort_values("snapshot_date").reset_index(drop=True)
        nav_sorted["portfolio_daily_return_pct"] = (
            nav_sorted["portfolio_value"].pct_change() * 100
        )
        nav_sorted["qqq_daily_return_pct"] = (
            nav_sorted["qqq_close"].pct_change() * 100
            if "qqq_close" in nav_sorted else None
        )

        col_left, col_right = st.columns([2, 1])
        with col_left:
            st.subheader("Daily returns — Portfolio vs QQQ")
            fig2 = go.Figure()
            fig2.add_trace(go.Bar(
                x=nav_sorted["snapshot_date"],
                y=nav_sorted["portfolio_daily_return_pct"],
                name="Portfolio",
                marker_color="#1976d2",
            ))
            fig2.add_trace(go.Bar(
                x=nav_sorted["snapshot_date"],
                y=nav_sorted["qqq_daily_return_pct"],
                name="QQQ",
                marker_color="#9ca3af",
            ))
            fig2.update_layout(
                height=320, barmode="group",
                yaxis_title="Daily return %", xaxis_title=None,
                paper_bgcolor="white", plot_bgcolor="white",
                yaxis=dict(gridcolor="#e5e7eb"), xaxis=dict(gridcolor="#e5e7eb"),
                margin=dict(t=20, b=20, l=20, r=20),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            )
            st.plotly_chart(fig2, use_container_width=True)

        with col_right:
            positions = load_positions(db_path)
            st.subheader("Current allocation")
            if positions.empty:
                st.info("No positions yet.")
            else:
                pos_with_mv = positions.dropna(subset=["market_value"]).copy()
                if not pos_with_mv.empty:
                    fig3 = px.pie(
                        pos_with_mv,
                        values="market_value",
                        names="ticker",
                        color_discrete_sequence=px.colors.qualitative.Set2,
                    )
                    fig3.update_traces(textposition="inside", textinfo="percent+label")
                    fig3.update_layout(
                        height=320, showlegend=False,
                        paper_bgcolor="white",
                        margin=dict(t=20, b=20, l=20, r=20),
                    )
                    st.plotly_chart(fig3, use_container_width=True)
                else:
                    st.info("Positions present but no market values yet.")

    # Decision distribution
    decisions = load_decisions_summary(db_path)
    if not decisions.empty:
        st.subheader("Decisions by rating")
        counts = decisions["action"].str.title().value_counts().reindex(
            ["Buy", "Overweight", "Hold", "Underweight", "Sell"], fill_value=0
        )
        fig4 = go.Figure(go.Bar(
            x=counts.index,
            y=counts.values,
            marker_color=["#059669", "#7cb342", "#fbbf24", "#fb923c", "#dc2626"],
        ))
        fig4.update_layout(
            height=260,
            yaxis_title="Count", xaxis_title=None,
            paper_bgcolor="white", plot_bgcolor="white",
            yaxis=dict(gridcolor="#e5e7eb"),
            margin=dict(t=20, b=20, l=20, r=20),
            showlegend=False,
        )
        st.plotly_chart(fig4, use_container_width=True)

    # Tables below charts (collapsed by default).
    if not decisions.empty or not load_trades(db_path).empty:
        with st.expander("Recent decisions & trades"):
            tab_d, tab_t = st.tabs(["Decisions", "Trades"])
            with tab_d:
                st.dataframe(decisions, use_container_width=True, hide_index=True)
            with tab_t:
                st.dataframe(load_trades(db_path), use_container_width=True, hide_index=True)


# ─── Tab 2: Architecture ──────────────────────────────────────────────────────


_ARCH_DOT = """
digraph G {
  rankdir=TB;
  bgcolor="white";
  node [shape=box, style="filled,rounded", fontname="Helvetica", fontsize=11, color="#d1d5db"];
  edge [color="#9ca3af", arrowsize=0.6];

  ticker [label="Ticker\\n(NVDA, AAPL, …)", shape=oval, fillcolor="#1976d2", fontcolor=white, fontsize=12];

  subgraph cluster_analysts {
    label="Stage 1 — Analyst Team (parallel)"; style="rounded,dashed"; color="#9ca3af"; fontcolor="#374151";
    market [label="Market Analyst\\ntechnicals (RSI, MACD, SMA)", fillcolor="#dbeafe"];
    sentiment [label="Sentiment Analyst\\nReddit + X", fillcolor="#dbeafe"];
    news [label="News Analyst\\nBloomberg, Yahoo, EODHD", fillcolor="#dbeafe"];
    fundamentals [label="Fundamentals Analyst\\n10-K, ratios, FCF", fillcolor="#dbeafe"];
  }

  subgraph cluster_research {
    label="Stage 2 — Researcher Debate"; style="rounded,dashed"; color="#9ca3af"; fontcolor="#374151";
    bull [label="Bull Researcher", fillcolor="#fef3c7"];
    bear [label="Bear Researcher", fillcolor="#fef3c7"];
    research_mgr [label="Research Manager\\n(synthesizes debate)", fillcolor="#fde68a"];
  }

  trader [label="Stage 3 — Trader\\nproposes transaction", fillcolor="#d1fae5"];

  subgraph cluster_risk {
    label="Stage 4 — Risk Management"; style="rounded,dashed"; color="#9ca3af"; fontcolor="#374151";
    risky [label="Risky Analyst", fillcolor="#fee2e2"];
    neutral [label="Neutral Analyst", fillcolor="#fee2e2"];
    safe [label="Safe Analyst", fillcolor="#fee2e2"];
  }

  pm [label="Stage 5 — Portfolio Manager\\nFinal 5-tier rating", fillcolor="#1976d2", fontcolor=white, fontsize=12];

  translator [label="Our Translator\\nrating → $ sizing\\n(Buy 10% / OW 5% / cap 20%)", shape=box, fillcolor="#e0f2fe", color="#0369a1"];
  alpaca [label="Alpaca Paper Trading", shape=cylinder, fillcolor="#1f2937", fontcolor=white];

  state [label="SQLite\\n(decisions, trades, NAV)", shape=cylinder, fillcolor="#f3f4f6"];
  memory [label="Reflection Memory Log\\n(learns 5d-later vs QQQ)", shape=cylinder, fillcolor="#f3f4f6"];

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

  pm -> translator;
  translator -> alpaca;
  translator -> state [style=dashed];
  pm -> memory [style=dashed];
  memory -> pm [style=dotted, label="past_context\\non next run", fontsize=9];
}
"""


def render_architecture() -> None:
    st.markdown(
        "**11 LLM agents per ticker per day**, plus our deterministic translator and persistence layer. "
        "The reflection memory feeds learnings from past trades back into the Portfolio Manager."
    )
    st.graphviz_chart(_ARCH_DOT, use_container_width=True)

    st.markdown("### Per-stage purpose")
    st.markdown(
        """
| Stage | Agents | What they do |
|---|---|---|
| **1. Analyst Team** | 4 (parallel) | Gather data — technicals, sentiment, news, fundamentals |
| **2. Researcher Debate** | 2 | Bull and Bear argue. A Research Manager synthesizes. |
| **3. Trader** | 1 | Proposes a transaction with sizing + stop loss |
| **4. Risk Management** | 3 | Risky / Neutral / Safe re-debate the proposal |
| **5. Portfolio Manager** | 1 | Emits the final 5-tier rating (Buy / Overweight / Hold / Underweight / Sell) |
| **Translator** | deterministic | Maps rating → $ amount. **The LLM cannot override our risk caps.** |
| **Reflection** | 1 | After 5 trading days, looks at realized alpha vs QQQ and writes a post-mortem fed into future decisions |
"""
    )


# ─── Tab 3: Memory & Learning ─────────────────────────────────────────────────


def _default_memory_path() -> str:
    return os.environ.get(
        "TRADINGAGENTS_MEMORY_LOG_PATH",
        os.path.expanduser("~/.tradingagents/memory/trading_memory.md"),
    )


def _parse_memory_log(path: str) -> list[dict]:
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


def render_memory() -> None:
    path = _default_memory_path()
    st.caption(f"Source: `{path}`")

    entries = _parse_memory_log(path)
    if not entries:
        st.info(
            "Memory log is empty. Each new decision adds an entry; reflections are added "
            "automatically ~5 trading days later once price outcomes are available."
        )
        return

    resolved = [e for e in entries if e["status"] == "resolved"]
    pending = [e for e in entries if e["status"] == "pending"]

    c1, c2, c3 = st.columns(3)
    c1.metric("Total memories", len(entries))
    c2.metric("Resolved", len(resolved))
    c3.metric("Pending", len(pending))

    st.markdown(
        "Past lessons are fed to the Portfolio Manager before each new decision. "
        "Alpha is measured against **QQQ**."
    )

    if resolved:
        st.subheader("Resolved — what the agent learned")
        for e in reversed(resolved):
            with st.expander(
                f"{e['date']} · {e['ticker']} · {e['rating']} · alpha {e['alpha']}"
            ):
                st.markdown(e["body"])

    if pending:
        st.subheader("Pending — waiting for outcome")
        for e in reversed(pending):
            with st.expander(f"{e['date']} · {e['ticker']} · {e['rating']} (pending)"):
                st.markdown(e["body"])


# ─── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    db_path = os.environ.get("PORTFOLIO_DB_PATH", "./portfolio_state.db")
    starting_cash = float(os.environ.get("PORTFOLIO_STARTING_CASH", "100000"))

    label, sub = day_label()
    st.markdown(f"<div class='day-banner'>{label}</div>", unsafe_allow_html=True)
    st.markdown(f"<div class='day-sub'>{sub}</div>", unsafe_allow_html=True)

    tab_overview, tab_arch, tab_memory = st.tabs(["📈 Overview", "🧠 Architecture", "📚 Memory & Learning"])

    if not os.path.exists(db_path):
        with tab_overview:
            st.warning("No data yet — dashboard activates after the first scheduled run.")
            render_overview(db_path, starting_cash)
        with tab_arch:
            render_architecture()
        with tab_memory:
            render_memory()
        return

    with tab_overview:
        render_overview(db_path, starting_cash)
    with tab_arch:
        render_architecture()
    with tab_memory:
        render_memory()


if __name__ == "__main__":
    main()
