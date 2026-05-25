"""Daily scheduler: runs the universe, settles fills, snapshots NAV.

Run manually:
    .venv/bin/python -m portfolio.scheduler.runner --once
Or start the cron daemon (blocks, Ctrl-C to stop):
    .venv/bin/python -m portfolio.scheduler.runner

Cron expr comes from PORTFOLIO_RUN_CRON env (default: weekdays 4pm America/New_York).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import date, timedelta
from typing import Iterable

from dotenv import load_dotenv

from portfolio.executor.alpaca_client import AlpacaClient
from portfolio.run_live import run_one
from portfolio.state import store

DEFAULT_CRON = "0 16 * * 1-5"
DEFAULT_UNIVERSE = "AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA,AVGO,ORCL"
DEFAULT_CASH_PARK_TICKER = "QQQ"
# Park idle cash in QQQ above this $-threshold so we don't underperform from
# sitting in cash during a rally.
CASH_PARK_THRESHOLD = 500.0
_SETTLED_STATUSES = {"filled", "canceled", "cancelled", "rejected", "expired"}


def parse_universe(raw: str | None = None) -> list[str]:
    raw = raw if raw is not None else os.environ.get("PORTFOLIO_UNIVERSE", DEFAULT_UNIVERSE)
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def run_universe(
    tickers: Iterable[str], trade_date: str, *, dry_run: bool = False
) -> dict[str, int]:
    results: dict[str, int] = {}
    for ticker in tickers:
        print(f"\n=== {ticker} ===")
        try:
            results[ticker] = run_one(ticker, trade_date, dry_run=dry_run)
        except Exception as e:
            print(f"[scheduler] {ticker} crashed: {e!r}", file=sys.stderr)
            results[ticker] = 1
    return results


def settle_pending_orders(
    alpaca: AlpacaClient,
    conn: sqlite3.Connection,
    *,
    max_wait_s: int = 60,
    poll_interval_s: int = 3,
) -> None:
    rows = conn.execute(
        """
        SELECT id, alpaca_order_id FROM trades
        WHERE alpaca_order_id IS NOT NULL
          AND status NOT IN ('filled','canceled','cancelled','rejected','expired')
        """
    ).fetchall()
    deadline = time.time() + max_wait_s
    while rows and time.time() < deadline:
        unsettled = []
        for r in rows:
            try:
                order = alpaca.get_order(r["alpaca_order_id"])
            except Exception as e:
                print(f"[scheduler] poll failed for {r['alpaca_order_id']}: {e!r}")
                continue
            store.update_trade_status(
                conn,
                trade_id=r["id"],
                status=order.status,
                filled_qty=order.filled_qty,
                filled_avg_price=order.filled_avg_price,
            )
            if order.status not in _SETTLED_STATUSES:
                unsettled.append(r)
        rows = unsettled
        if rows:
            time.sleep(poll_interval_s)


def snapshot_eod(
    alpaca: AlpacaClient, conn: sqlite3.Connection, snapshot_date: str
) -> None:
    account = alpaca.get_account()
    positions = alpaca.get_positions()
    store.snapshot_positions(
        conn,
        snapshot_date=snapshot_date,
        positions=[
            {
                "ticker": p.ticker,
                "qty": p.qty,
                "avg_entry_price": p.avg_entry_price,
                "market_value": p.market_value,
                "unrealized_pl": p.unrealized_pl,
            }
            for p in positions
        ],
    )
    qqq_close = _index_close_for("QQQ", snapshot_date)
    spy_close = _index_close_for("SPY", snapshot_date)
    store.log_nav(
        conn,
        snapshot_date=snapshot_date,
        portfolio_value=account.portfolio_value,
        cash=account.cash,
        equity=account.equity,
        spy_close=spy_close,
        qqq_close=qqq_close,
    )
    print(
        f"[scheduler] EOD snapshot {snapshot_date}: "
        f"nav=${account.portfolio_value:,.2f} qqq_close={qqq_close} spy_close={spy_close}"
    )


def park_idle_cash(
    alpaca: AlpacaClient,
    conn: sqlite3.Connection,
    *,
    park_ticker: str | None = None,
    threshold: float = CASH_PARK_THRESHOLD,
) -> None:
    """If cash > threshold, buy the park ticker (default QQQ) with the excess."""
    park_ticker = park_ticker or os.environ.get(
        "PORTFOLIO_CASH_PARK_TICKER", DEFAULT_CASH_PARK_TICKER
    )
    account = alpaca.get_account()
    if account.cash < threshold:
        print(
            f"[scheduler] cash ${account.cash:,.2f} below threshold "
            f"${threshold:,.0f}; no park needed."
        )
        return

    notional = round(account.cash * 0.99, 2)  # leave a tiny float for fees
    print(
        f"[scheduler] parking ${notional:,.2f} of idle cash in {park_ticker}..."
    )
    trade_id = store.log_trade(
        conn,
        decision_id=None,
        ticker=park_ticker,
        side="buy",
        qty=0,
        order_type="market",
    )
    try:
        order = alpaca.submit_market_order(park_ticker, "buy", notional=notional)
    except Exception as e:
        store.update_trade_status(
            conn, trade_id=trade_id, status="rejected", error=str(e)
        )
        print(f"[scheduler] cash park failed: {e!r}")
        return
    store.update_trade_status(
        conn,
        trade_id=trade_id,
        status=order.status,
        filled_qty=order.filled_qty,
        filled_avg_price=order.filled_avg_price,
    )
    print(f"[scheduler] park order {order.alpaca_order_id} status={order.status}")


def _index_close_for(ticker: str, iso_date: str) -> float | None:
    import yfinance as yf

    start = date.fromisoformat(iso_date)
    end = start + timedelta(days=1)
    hist = yf.Ticker(ticker).history(
        start=start.isoformat(), end=end.isoformat(), auto_adjust=False
    )
    if hist.empty:
        return None
    return float(hist["Close"].iloc[0])


def daily_job(*, dry_run: bool = False) -> None:
    today = date.today().isoformat()
    print(f"\n[scheduler] === daily run for {today} (dry_run={dry_run}) ===")
    universe = parse_universe()
    print(f"[scheduler] universe: {universe}")

    results = run_universe(universe, today, dry_run=dry_run)
    print(f"[scheduler] per-ticker exit codes: {results}")

    if dry_run:
        print("[scheduler] dry-run: skipping Alpaca settle + EOD snapshot.")
        return

    db_path = os.environ.get("PORTFOLIO_DB_PATH", "./portfolio_state.db")
    conn = store.connect(db_path)
    store.init_schema(conn)

    alpaca = AlpacaClient(paper=True)
    settle_pending_orders(alpaca, conn)
    park_idle_cash(alpaca, conn)
    settle_pending_orders(alpaca, conn)  # poll the park order too
    snapshot_eod(alpaca, conn, today)


def start_scheduler(cron_expr: str | None = None) -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    cron_expr = cron_expr or os.environ.get("PORTFOLIO_RUN_CRON", DEFAULT_CRON)
    parts = cron_expr.split()
    if len(parts) != 5:
        raise ValueError(f"PORTFOLIO_RUN_CRON must be a 5-field cron expr, got: {cron_expr!r}")
    minute, hour, dom, month, dow = parts

    trigger = CronTrigger(
        minute=minute,
        hour=hour,
        day=dom,
        month=month,
        day_of_week=dow,
        timezone="America/New_York",
    )
    sched = BlockingScheduler()
    sched.add_job(daily_job, trigger=trigger, id="daily_run", max_instances=1)
    print(
        f"[scheduler] daemon started; cron='{cron_expr}' (America/New_York). "
        "Press Ctrl-C to stop."
    )
    sched.start()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Daily TradingAgents → Alpaca scheduler.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run today's job once and exit (instead of starting the cron daemon).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Skip Alpaca calls.")
    args = parser.parse_args(argv)

    if args.once:
        daily_job(dry_run=args.dry_run)
        return 0
    start_scheduler()
    return 0


if __name__ == "__main__":
    sys.exit(main())
