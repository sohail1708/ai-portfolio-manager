"""TradingAgentsGraph subclass that benchmarks against QQQ instead of SPY.

Upstream's ``_fetch_returns`` hard-codes SPY as the alpha benchmark when
generating reflections — see tradingagents/graph/trading_graph.py:205.
For this project the goal is to beat QQQ (NASDAQ-100), so we override the
single method that names the benchmark ticker. The rest of the reflection
loop (memory log, past-context injection into the Portfolio Manager,
deferred resolution on the next same-ticker run) is unchanged.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple

import yfinance as yf

from tradingagents.graph.trading_graph import TradingAgentsGraph

logger = logging.getLogger(__name__)

BENCHMARK_TICKER = "QQQ"


class QQQAwareGraph(TradingAgentsGraph):
    """Same as upstream, except reflections measure alpha vs QQQ, not SPY."""

    def _fetch_returns(
        self, ticker: str, trade_date: str, holding_days: int = 5
    ) -> Tuple[Optional[float], Optional[float], Optional[int]]:
        try:
            start = datetime.strptime(trade_date, "%Y-%m-%d")
            end = start + timedelta(days=holding_days + 7)
            end_str = end.strftime("%Y-%m-%d")

            stock = yf.Ticker(ticker).history(start=trade_date, end=end_str)
            bench = yf.Ticker(BENCHMARK_TICKER).history(start=trade_date, end=end_str)

            if len(stock) < 2 or len(bench) < 2:
                return None, None, None

            actual_days = min(holding_days, len(stock) - 1, len(bench) - 1)
            raw = float(
                (stock["Close"].iloc[actual_days] - stock["Close"].iloc[0])
                / stock["Close"].iloc[0]
            )
            bench_ret = float(
                (bench["Close"].iloc[actual_days] - bench["Close"].iloc[0])
                / bench["Close"].iloc[0]
            )
            alpha = raw - bench_ret
            return raw, alpha, actual_days
        except Exception as e:
            logger.warning(
                "Could not resolve outcome for %s on %s (will retry next run): %s",
                ticker, trade_date, e,
            )
            return None, None, None
