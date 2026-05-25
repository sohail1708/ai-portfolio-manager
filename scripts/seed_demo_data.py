"""Seed portfolio_state.db with realistic-looking demo data so the dashboard
has something to render before the live run starts.

Run:
    .venv/bin/python scripts/seed_demo_data.py

Wipes any existing DB at PORTFOLIO_DB_PATH and writes 3 trading days of
sample decisions, trades, NAV, and benchmark data.
"""

from __future__ import annotations

import os
import pathlib

from portfolio.state import store

DEMO_AGENT_REPORTS = {
    "NVDA-2026-06-01": {
        "market_report": "NVDA is in a strong uptrend, trading above the 50-day and 200-day SMAs. RSI at 62 (not yet overbought). MACD positive with widening histogram. Volume on up days exceeds down days by 1.4x over the past two weeks. Bollinger Bands suggest room to run before mean reversion.",
        "sentiment_report": "Reddit r/wallstreetbets sentiment skew +0.42 (positive). X/Twitter mentions up 28% week-over-week, with bullish-to-bearish ratio of 3.1:1. Notable accounts highlighting Q1 earnings beat and AI infrastructure demand.",
        "news_report": "Three positive catalysts this week: (1) Confirmed expansion of Blackwell GPU contracts with Microsoft Azure, (2) Beat Q1 revenue by $1.2B, (3) Upgraded by Goldman to 'Conviction Buy' with $180 PT.",
        "fundamentals_report": "Revenue +69% YoY. Operating margins held at 64%, FCF margin 41%. P/E 32x on forward 12mo earnings — premium but justified by growth. ROE 114%. Cash position $34B, net debt negative.",
        "investment_debate_state": {
            "current_response": "Bull case: AI infrastructure is in the early innings; NVDA's CUDA moat means peers can't catch up for 3+ years. Estimates have been consistently revised upward.",
            "bear_history": "Bear case: Valuation already prices in continued perfection. If hyperscalers slow capex in 2H26, NVDA could re-rate sharply. Concentration risk: top 4 customers = 53% of revenue.",
        },
        "investment_plan": "Overweight stance recommended. NVDA remains best-in-class for AI infrastructure exposure but valuation suggests staggered accumulation. Target 4-5% NAV with stops at $98 (recent shakeout low). Add on dips to 50-day SMA.",
        "trader_investment_plan": "Action: Buy. Reasoning: Technical setup remains constructive, fundamentals support the thesis, sentiment is positive without being euphoric. Stop loss: 98.00. Position sizing: target 4-5% NAV.",
        "risk_debate_state": {
            "current_aggressive_response": "Lean in. The AI cycle has multiple years to run. We should be overweight on conviction.",
            "current_neutral_response": "Buy here is appropriate. Position sizing should respect the concentration in our broader portfolio.",
            "current_conservative_response": "Concern about beta — NVDA at 2.2 means a 10% market drawdown takes us down 22%. Consider half-position.",
        },
        "final_trade_decision": "**Rating**: Buy\n\n**Executive Summary**: Strong AI demand, elite margins, momentum intact across all three timeframes.\n\n**Investment Thesis**: NVDA remains the dominant supplier of AI infrastructure. Even with premium valuation, growth trajectory justifies continued accumulation.\n\n**Price Target**: 180.00\n\n**Time Horizon**: 3-6 months",
    },
    "AAPL-2026-06-01": {
        "market_report": "AAPL consolidating in a tight range between $215 and $228. RSI neutral at 51. MACD flattening. Below 50-day SMA but above 200-day. Volume profile suggests accumulation at the lower bound.",
        "sentiment_report": "Sentiment mixed. Vision Pro reception slowing; iPhone 17 leaks suggesting incremental upgrades. Sentiment skew -0.05 (slightly negative). Notable analyst downgrades from Morgan Stanley.",
        "news_report": "Mixed week: WWDC pushed back AI announcement timeline, Services growth decelerating to 11% (vs 16% prior). Offset by buyback announcement of $90B.",
        "fundamentals_report": "Revenue +4% YoY (slowest in 4 quarters). Operating margins steady at 30%. P/E 28x. Cash $156B. Services as % of revenue rising slowly.",
        "investment_debate_state": {
            "current_response": "Bull: AAPL's installed base of 2.2B devices is a moat that compounds. Services revenue is high-margin and resilient.",
            "bear_history": "Bear: Growth has stalled. Vision Pro is a flop. Without AI catalyst, multiple compression risk is real.",
        },
        "investment_plan": "Hold rating. Setup is neutral — no compelling catalyst either way. Wait for clearer signal post-WWDC.",
        "trader_investment_plan": "Action: Hold. No high-conviction reason to add or trim. Maintain existing position if any.",
        "risk_debate_state": {
            "current_aggressive_response": "Disagree — AAPL is a value play here. Multiple has compressed enough.",
            "current_neutral_response": "Hold is appropriate. Wait for the WWDC catalyst.",
            "current_conservative_response": "Hold or trim — better risk/reward elsewhere in the universe.",
        },
        "final_trade_decision": "**Rating**: Hold\n\n**Executive Summary**: Stalled growth and no near-term catalyst.\n\n**Investment Thesis**: Wait for WWDC to provide direction; current setup is balanced.",
    },
    "MSFT-2026-06-02": {
        "market_report": "MSFT broke out of a 4-week consolidation to the upside. Closed at all-time high. RSI 68 (approaching overbought). MACD positive. Volume on breakout exceeded 20-day average by 32%.",
        "sentiment_report": "Sentiment strongly positive on Copilot adoption and Azure AI revenue. Sentiment skew +0.51. Build conference takeaways universally bullish.",
        "news_report": "Two major announcements: (1) Azure AI revenue run-rate now $7.4B annualized, (2) Anthropic partnership renewal at expanded scope. Both well-received.",
        "fundamentals_report": "Revenue +13% YoY. Cloud (Azure) growing +27%. Operating margin 45% — sector-leading. P/E 36x. Net cash $74B.",
        "investment_debate_state": {
            "current_response": "Bull: MSFT is the clearest enterprise AI play. Azure + Copilot + OpenAI partnership creates a flywheel.",
            "bear_history": "Bear: 36x P/E is rich. Capex spend is enormous and ROI on AI infra is unproven.",
        },
        "investment_plan": "Buy rating with high conviction. Best-in-class moat across cloud and enterprise AI. Target 5%+ NAV.",
        "trader_investment_plan": "Action: Buy. Reasoning: Breakout confirmed, fundamentals improving, sentiment shifted positive. Stop loss: 410.00. Position sizing: 5%.",
        "risk_debate_state": {
            "current_aggressive_response": "Full position here. This is the highest-conviction name in our universe.",
            "current_neutral_response": "Buy at 5% NAV is right. Don't oversize given RSI nearing 70.",
            "current_conservative_response": "Buy is OK but stop loss should be tighter; momentum can reverse fast at these levels.",
        },
        "final_trade_decision": "**Rating**: Buy\n\n**Executive Summary**: Enterprise AI flywheel confirmed; breakout above multi-week range.\n\n**Investment Thesis**: Azure + Copilot is the highest-quality AI exposure in the universe.\n\n**Price Target**: 510.00\n\n**Time Horizon**: 6-12 months",
    },
    "GOOGL-2026-06-02": {
        "market_report": "GOOGL down 3.4% on the day on antitrust headline. Broke below 50-day SMA. RSI 38 (approaching oversold). MACD negative.",
        "sentiment_report": "Sentiment sharply negative this week. Sentiment skew -0.38. Heavy short interest building.",
        "news_report": "DOJ proposes structural remedies including divestiture of Chrome. Search market share down 3.2pp YoY to ~88%.",
        "fundamentals_report": "Revenue +14% YoY, Search +9%, Cloud +28%. Margins compressed slightly. P/E 22x.",
        "investment_debate_state": {
            "current_response": "Bull: Selloff overdone — DOJ remedies likely settled with concessions, not divestiture. GenAI search products gaining.",
            "bear_history": "Bear: Antitrust risk is real and undiscounted. Multiple compression has further to run.",
        },
        "investment_plan": "Underweight. Headline risk dominates near-term. Re-evaluate after DOJ ruling.",
        "trader_investment_plan": "Action: Sell or trim. Reduce exposure pending clarity on DOJ remedies.",
        "risk_debate_state": {
            "current_aggressive_response": "Counter-take: this is the time to BUY. Selloffs on legal headlines historically recover.",
            "current_neutral_response": "Trim makes sense. Cut position by 50% but keep core exposure.",
            "current_conservative_response": "Underweight or out entirely. Headline risk skewed asymmetrically negative.",
        },
        "final_trade_decision": "**Rating**: Underweight\n\n**Executive Summary**: Near-term antitrust risk warrants reduced exposure.\n\n**Investment Thesis**: Trim until DOJ remedies are clarified; re-evaluate after ruling.",
    },
}


def _seed_decision(conn, ticker: str, trade_date: str) -> int:
    key = f"{ticker}-{trade_date}"
    state = DEMO_AGENT_REPORTS.get(key, {})
    rating = "Hold"
    if "Buy" in state.get("final_trade_decision", ""):
        rating = "Buy"
    elif "Overweight" in state.get("final_trade_decision", ""):
        rating = "Overweight"
    elif "Underweight" in state.get("final_trade_decision", ""):
        rating = "Underweight"
    elif "Sell" in state.get("final_trade_decision", ""):
        rating = "Sell"

    return store.log_decision(
        conn,
        ticker=ticker,
        trade_date=trade_date,
        action=rating,
        reasoning=state.get("investment_plan"),
        stop_loss=None,
        position_sizing=None,
        raw_state=state,
    )


def main() -> None:
    db_path = os.environ.get("PORTFOLIO_DB_PATH", "./portfolio_state.db")
    pathlib.Path(db_path).unlink(missing_ok=True)

    conn = store.connect(db_path)
    store.init_schema(conn)

    # Day 1 — 2026-06-01: NVDA buy, AAPL hold
    nvda_id = _seed_decision(conn, "NVDA", "2026-06-01")
    aapl_id = _seed_decision(conn, "AAPL", "2026-06-01")

    store.log_trade(
        conn, decision_id=nvda_id, ticker="NVDA", side="buy", qty=0, order_type="market",
        alpaca_order_id="demo-nvda-001",
    )
    # Mark trade filled
    conn.execute(
        "UPDATE trades SET status='filled', filled_qty=98.5, filled_avg_price=101.45 WHERE alpaca_order_id='demo-nvda-001'"
    )
    conn.commit()

    store.snapshot_positions(
        conn,
        snapshot_date="2026-06-01",
        positions=[
            {"ticker": "NVDA", "qty": 98.5, "avg_entry_price": 101.45, "market_value": 9_990.0, "unrealized_pl": -12.0},
            {"ticker": "QQQ", "qty": 168.0, "avg_entry_price": 535.50, "market_value": 89_964.0, "unrealized_pl": 0.0},
        ],
    )
    store.log_nav(
        conn, snapshot_date="2026-06-01",
        portfolio_value=99_954.0, cash=0.0, equity=99_954.0,
        spy_close=580.50, qqq_close=535.50,
    )

    # Day 2 — 2026-06-02: MSFT buy, GOOGL underweight
    msft_id = _seed_decision(conn, "MSFT", "2026-06-02")
    googl_id = _seed_decision(conn, "GOOGL", "2026-06-02")

    store.log_trade(
        conn, decision_id=msft_id, ticker="MSFT", side="buy", qty=0, order_type="market",
        alpaca_order_id="demo-msft-002",
    )
    conn.execute(
        "UPDATE trades SET status='filled', filled_qty=11.85, filled_avg_price=421.30 WHERE alpaca_order_id='demo-msft-002'"
    )
    store.log_trade(
        conn, decision_id=googl_id, ticker="GOOGL", side="sell", qty=0, order_type="market",
        alpaca_order_id="demo-googl-002",
    )
    conn.execute(
        "UPDATE trades SET status='filled', filled_qty=15.0, filled_avg_price=178.40 WHERE alpaca_order_id='demo-googl-002'"
    )
    conn.commit()

    store.snapshot_positions(
        conn,
        snapshot_date="2026-06-02",
        positions=[
            {"ticker": "NVDA", "qty": 98.5, "avg_entry_price": 101.45, "market_value": 10_240.0, "unrealized_pl": 237.0},
            {"ticker": "MSFT", "qty": 11.85, "avg_entry_price": 421.30, "market_value": 5_010.0, "unrealized_pl": 18.0},
            {"ticker": "QQQ", "qty": 159.0, "avg_entry_price": 535.50, "market_value": 85_858.0, "unrealized_pl": 750.0},
        ],
    )
    store.log_nav(
        conn, snapshot_date="2026-06-02",
        portfolio_value=101_108.0, cash=0.0, equity=101_108.0,
        spy_close=582.10, qqq_close=540.30,
    )

    # Day 3 — 2026-06-03: no new decisions, just NAV move
    store.snapshot_positions(
        conn,
        snapshot_date="2026-06-03",
        positions=[
            {"ticker": "NVDA", "qty": 98.5, "avg_entry_price": 101.45, "market_value": 10_430.0, "unrealized_pl": 427.0},
            {"ticker": "MSFT", "qty": 11.85, "avg_entry_price": 421.30, "market_value": 5_080.0, "unrealized_pl": 88.0},
            {"ticker": "QQQ", "qty": 159.0, "avg_entry_price": 535.50, "market_value": 86_280.0, "unrealized_pl": 1_172.0},
        ],
    )
    store.log_nav(
        conn, snapshot_date="2026-06-03",
        portfolio_value=101_790.0, cash=0.0, equity=101_790.0,
        spy_close=583.20, qqq_close=542.95,
    )

    print(f"seeded demo data into {db_path}")
    print("  agent_decisions:", conn.execute("SELECT COUNT(*) FROM agent_decisions").fetchone()[0])
    print("  trades:         ", conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0])
    print("  positions rows: ", conn.execute("SELECT COUNT(*) FROM positions_snapshot").fetchone()[0])
    print("  nav rows:       ", conn.execute("SELECT COUNT(*) FROM nav_history").fetchone()[0])


if __name__ == "__main__":
    main()
