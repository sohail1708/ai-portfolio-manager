"""Single-ticker entry point: TradingAgents → translator → Alpaca → state store.

Usage:
    .venv/bin/python -m portfolio.run_live NVDA
    .venv/bin/python -m portfolio.run_live NVDA --date 2026-05-25
    .venv/bin/python -m portfolio.run_live NVDA --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date

from dotenv import load_dotenv

from portfolio.agents.qqq_aware_graph import QQQAwareGraph
from portfolio.executor.alpaca_client import AlpacaClient
from portfolio.executor.translator import (
    OrderIntent,
    TranslatorConfig,
    TranslatorContext,
    parse_decision,
    translate,
)
from portfolio.state import store
from tradingagents.default_config import DEFAULT_CONFIG


def run_one(ticker: str, trade_date: str, *, dry_run: bool = False) -> int:
    config = DEFAULT_CONFIG.copy()
    config["quick_think_llm"] = "gpt-5.4-mini"
    config["deep_think_llm"] = "gpt-5"
    config["max_debate_rounds"] = 1
    config["data_vendors"] = {
        "core_stock_apis": "yfinance",
        "technical_indicators": "yfinance",
        "fundamental_data": "yfinance",
        "news_data": "yfinance",
    }

    print(f"[run_live] {ticker} on {trade_date} (dry_run={dry_run})")

    print("[run_live] running TradingAgents pipeline (QQQ-aware reflection)...")
    ta = QQQAwareGraph(debug=False, config=config)
    final_state, _ = ta.propagate(ticker, trade_date)
    decision_text = final_state["final_trade_decision"]

    decision = parse_decision(decision_text)
    print(
        f"[run_live] parsed rating={decision.rating} "
        f"price_target={decision.price_target} horizon={decision.time_horizon}"
    )

    db_path = os.environ.get("PORTFOLIO_DB_PATH", "./portfolio_state.db")
    conn = store.connect(db_path)
    store.init_schema(conn)

    # Persist every agent's intermediate output so the dashboard can render
    # the per-day reasoning flow (analyst reports, debate, trader, risk, PM).
    persisted_state = {
        k: v
        for k, v in final_state.items()
        if k != "messages"  # raw LangChain messages bloat the JSON; not needed
    }

    decision_id = store.log_decision(
        conn,
        ticker=ticker,
        trade_date=trade_date,
        action=decision.rating,
        reasoning=decision.investment_thesis or decision.executive_summary,
        stop_loss=None,
        position_sizing=decision.time_horizon,
        raw_state=persisted_state,
    )
    print(f"[run_live] logged decision id={decision_id}")

    if decision.rating == "Hold":
        print("[run_live] HOLD — no order.")
        return 0

    if dry_run:
        print("[run_live] dry-run: skipping Alpaca account fetch + order submission.")
        return 0

    alpaca = AlpacaClient(paper=True)
    account = alpaca.get_account()
    current_position = alpaca.get_position(ticker)

    intent = translate(
        ticker=ticker,
        decision=decision,
        ctx=TranslatorContext(
            nav=account.portfolio_value,
            cash=account.cash,
            buying_power=account.buying_power,
            current_position=current_position,
        ),
        cfg=TranslatorConfig(),
    )

    if intent is None:
        print("[run_live] translator returned no-op (cap, dust, or no position to sell).")
        return 0

    print(f"[run_live] submitting order: {intent}")
    return _submit_and_log(alpaca, conn, decision_id, intent)


def _submit_and_log(
    alpaca: AlpacaClient,
    conn,
    decision_id: int,
    intent: OrderIntent,
) -> int:
    trade_id = store.log_trade(
        conn,
        decision_id=decision_id,
        ticker=intent.ticker,
        side=intent.side,
        qty=intent.qty or 0,
        order_type="market",
    )
    try:
        order = alpaca.submit_market_order(
            intent.ticker, intent.side, qty=intent.qty, notional=intent.notional
        )
    except Exception as e:
        store.update_trade_status(
            conn, trade_id=trade_id, status="rejected", error=str(e)
        )
        print(f"[run_live] Alpaca rejected order: {e}", file=sys.stderr)
        return 1

    store.update_trade_status(
        conn,
        trade_id=trade_id,
        status=order.status,
        filled_qty=order.filled_qty,
        filled_avg_price=order.filled_avg_price,
    )
    print(
        f"[run_live] submitted alpaca_order_id={order.alpaca_order_id} "
        f"status={order.status}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Run TradingAgents for one ticker, optionally trade.")
    parser.add_argument("ticker", help="Ticker symbol, e.g. NVDA")
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="Trade date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the LLM pipeline + log decision, but skip Alpaca order submission.",
    )
    args = parser.parse_args(argv)

    return run_one(args.ticker.upper(), args.date, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
