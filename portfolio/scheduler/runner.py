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
# Market opens 09:30 ET; poll a few minutes later so submitted-overnight orders
# have a chance to fill before we update the DB and dashboard.
DEFAULT_OPEN_CRON = "35 9 * * 1-5"
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
    """If cash > threshold, buy the park ticker (default QQQ) with the excess.

    Critical: the park amount must NOT include cash that's already committed
    to pending buy orders submitted earlier in the same run. Otherwise we'd
    over-commit cash (e.g., $40k of pending stock buys + a $79k park against
    $80k cash → some orders bounce at next open).

    We compute available cash as `cash - sum(open buy notional)` and park
    ~95% of that, leaving a small float for slippage / partial fills.
    """
    park_ticker = park_ticker or os.environ.get(
        "PORTFOLIO_CASH_PARK_TICKER", DEFAULT_CASH_PARK_TICKER
    )
    account = alpaca.get_account()

    # Subtract any pending buy commitments so the park doesn't compete with
    # orders submitted earlier in this same daily run.
    try:
        open_orders = alpaca.list_open_orders()
    except Exception as e:
        print(f"[scheduler] could not fetch open orders, falling back to cash: {e!r}")
        open_orders = []
    committed = sum(
        (o.get("notional") or 0)
        for o in open_orders
        if o.get("side") == "buy"
    )
    available = max(0.0, account.cash - committed)

    if available < threshold:
        print(
            f"[scheduler] available cash ${available:,.2f} "
            f"(cash ${account.cash:,.2f} − pending buys ${committed:,.2f}) "
            f"below threshold ${threshold:,.0f}; no park needed."
        )
        return

    # 95% leaves room for slippage / partial fills of the sibling stock buys.
    notional = round(available * 0.95, 2)
    print(
        f"[scheduler] parking ${notional:,.2f} of idle cash in {park_ticker} "
        f"(cash ${account.cash:,.2f} − pending buys ${committed:,.2f} = ${available:,.2f} available)..."
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


def morning_fills_job() -> None:
    """Post-open: re-poll yesterday's queued orders and refresh positions.

    Runs ~5 min after the 09:30 ET open. The previous evening's market-close
    orders (submitted ~16:00 ET) sit in 'accepted' overnight and fill at open,
    so the DB and positions snapshot are stale until we re-poll here.
    """
    today = date.today().isoformat()
    print(f"\n[scheduler] === morning fills job for {today} ===")
    db_path = os.environ.get("PORTFOLIO_DB_PATH", "./portfolio_state.db")
    conn = store.connect(db_path)
    store.init_schema(conn)

    alpaca = AlpacaClient(paper=True)
    # Give late fills extra time — same poll loop as EOD but longer budget.
    settle_pending_orders(alpaca, conn, max_wait_s=180)

    # Refresh positions snapshot (overwrites today's row via UPSERT). Skip
    # NAV row here so we don't clobber yesterday's EOD close prices — full
    # EOD snapshot at 16:00 ET will write today's NAV row.
    positions = alpaca.get_positions()
    store.snapshot_positions(
        conn,
        snapshot_date=today,
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
    account = alpaca.get_account()
    print(
        f"[scheduler] morning refresh: positions={len(positions)} "
        f"cash=${account.cash:,.2f} equity=${account.equity:,.2f}"
    )


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


def _cron_trigger(expr: str):
    from apscheduler.triggers.cron import CronTrigger

    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron expr must have 5 fields, got: {expr!r}")
    minute, hour, dom, month, dow = parts
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=dom,
        month=month,
        day_of_week=dow,
        timezone="America/New_York",
    )


def start_scheduler(
    cron_expr: str | None = None, open_cron_expr: str | None = None
) -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler

    cron_expr = cron_expr or os.environ.get("PORTFOLIO_RUN_CRON", DEFAULT_CRON)
    open_cron_expr = open_cron_expr or os.environ.get(
        "PORTFOLIO_OPEN_CRON", DEFAULT_OPEN_CRON
    )

    sched = BlockingScheduler()
    sched.add_job(
        daily_job, trigger=_cron_trigger(cron_expr), id="daily_run", max_instances=1
    )
    sched.add_job(
        morning_fills_job,
        trigger=_cron_trigger(open_cron_expr),
        id="morning_fills",
        max_instances=1,
    )
    print(
        f"[scheduler] daemon started; daily_run='{cron_expr}' "
        f"morning_fills='{open_cron_expr}' (America/New_York). "
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
    parser.add_argument(
        "--morning-fills",
        action="store_true",
        help="Run only the post-open fills-polling + positions-refresh job and exit.",
    )
    args = parser.parse_args(argv)

    if args.morning_fills:
        morning_fills_job()
        return 0
    if args.once:
        daily_job(dry_run=args.dry_run)
        return 0
    start_scheduler()
    return 0


if __name__ == "__main__":
    sys.exit(main())
