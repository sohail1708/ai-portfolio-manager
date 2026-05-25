"""Manual smoke test for portfolio.executor.alpaca_client.

Run after setting ALPACA_API_KEY and ALPACA_SECRET_KEY in .env:
    .venv/bin/python scripts/alpaca_smoke.py

Submits a $1 fractional BUY of SPY, polls for fill, prints the result.
Paper account only — refuses to run if base URL is live.
"""

from __future__ import annotations

import os
import sys
import time

from dotenv import load_dotenv

from portfolio.executor.alpaca_client import AlpacaClient


def main() -> int:
    load_dotenv()

    base_url = os.environ.get("ALPACA_BASE_URL", "")
    if "paper" not in base_url.lower():
        print(
            f"Refusing to run: ALPACA_BASE_URL={base_url!r} doesn't look like paper.",
            file=sys.stderr,
        )
        return 2

    client = AlpacaClient(paper=True)

    print("== Account ==")
    acct = client.get_account()
    print(f"  cash:           ${acct.cash:,.2f}")
    print(f"  equity:         ${acct.equity:,.2f}")
    print(f"  buying_power:   ${acct.buying_power:,.2f}")
    print(f"  portfolio_val:  ${acct.portfolio_value:,.2f}")

    print("\n== Latest SPY price ==")
    px = client.get_latest_price("SPY")
    print(f"  ${px:,.2f}")

    print("\n== Submitting $1 notional BUY of SPY ==")
    order = client.submit_market_order("SPY", "buy", notional=1.0)
    print(f"  order id: {order.alpaca_order_id}")
    print(f"  initial status: {order.status}")

    print("\n== Polling for fill (up to 10s) ==")
    for _ in range(10):
        time.sleep(1)
        order = client.get_order(order.alpaca_order_id)
        print(f"  status={order.status} filled_qty={order.filled_qty}")
        if order.status in {"filled", "rejected", "canceled"}:
            break

    print("\n== Positions after ==")
    for p in client.get_positions():
        print(f"  {p.ticker}: qty={p.qty} avg=${p.avg_entry_price:.2f} mv=${p.market_value:.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
